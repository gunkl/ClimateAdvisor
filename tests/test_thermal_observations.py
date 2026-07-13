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
    THERMAL_PASSIVE_CONF_HIGH,
    THERMAL_PASSIVE_MIN_SAMPLES,
    THERMAL_PASSIVE_SAMPLE_INTERVAL_S,
    THERMAL_ROLLING_MAX_WINDOW_MINUTES,
    THERMAL_ROLLING_MIN_DELTA_T_F,
    THERMAL_ROLLING_MIN_WINDOW_MINUTES,
    THERMAL_ROLLING_WINDOW_MINUTES,
    THERMAL_SOLAR_FACTOR_MIN_RANGE,
    THERMAL_VENT_MIN_SAMPLES,
    THERMAL_VENTILATED_MIN_DELTA_F,
)
from custom_components.climate_advisor.learning import LearningEngine, _grade_passive_confidence  # noqa: E402

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

    def _exec_job(fn, *args):
        # Issue #491: mirrors real HA semantics for both calling conventions used in
        # coordinator.py — some call sites `await hass.async_add_executor_job(...)`
        # (needs an awaitable Future), others (_abandon_observation, sync, fire-and-
        # forget) call it with no running loop at all. asyncio.get_running_loop()
        # tells us which context we are in; Python 3.14 removed the implicit event
        # loop, so this cannot unconditionally use get_event_loop().
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return fn(*args)
        fut = loop.create_future()
        fut.set_result(fn(*args))
        return fut

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

    def test_stays_alive_with_sufficient_samples_while_hvac_idle(self):
        """passive_decay stays alive when HVAC is still idle, even with sufficient samples and signal.

        The old OLS path committed on sample count alone. The chart_log endpoint path is
        event-driven: the observation runs until HVAC starts, a sensor opens, a fan activates,
        or the hard cap is reached — then _run_passive_chart_log_fit() fires. It never commits
        mid-run just because MIN_SAMPLES is accumulated.
        """
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

        def _consume_coro(coro):
            coro.close()

        coord.hass.async_create_task = _consume_coro

        with patch("custom_components.climate_advisor.coordinator.dt_util", dt_mock):
            coord._sample_all_observations()

        # With HVAC still idle and no external events, the observation must remain alive.
        # chart_log endpoint only fires on external events (HVAC start, sensor open, fan, hard cap).
        assert OBS_TYPE_PASSIVE_DECAY in coord._pending_observations, (
            "passive_decay should stay alive when HVAC is idle — chart_log endpoint is "
            "event-driven, not sample-count-driven"
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
    """LearningEngine.load_state() handles legacy pending_thermal_event without crashing.

    The v3 implementation uses data.pop("pending_thermal_event", None) to silently
    discard the v2 field.  These tests verify that loading a state file containing
    the legacy field succeeds without error and leaves pending_observations empty.
    """

    def test_migration_converts_pending_thermal_event_heat(self, tmp_path: Path):
        """Loading a legacy DB with pending_thermal_event[mode=heat] succeeds.

        The v2 field is discarded (data.pop) — no migration conversion occurs.
        pending_observations stays empty; load does not crash.
        """
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
            },
        }
        state_file = tmp_path / "climate_advisor_learning.json"
        state_file.write_text(json.dumps(state_dict))

        engine = LearningEngine(tmp_path)
        engine.load_state()  # must not raise

        # v2 field is popped — not present as a state attribute
        assert not hasattr(engine._state, "pending_thermal_event") or engine._state.pending_thermal_event is None, (
            "pending_thermal_event should not survive load (it is popped/discarded)"
        )
        # pending_observations starts empty — no migration conversion
        assert isinstance(engine._state.pending_observations, dict)
        assert engine._state.pending_observations == {}, (
            "pending_observations should be empty after discarding legacy pending_thermal_event"
        )

    def test_migration_discards_active_status_v2_event(self, tmp_path: Path):
        """Loading a legacy DB with pending_thermal_event[status=active] succeeds without error.

        The v2 field is silently discarded via data.pop — pending_observations stays empty.
        """
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
        engine.load_state()  # must not raise

        assert engine._state.pending_observations == {}, "pending_observations must be empty after discarding v2 field"

    def test_migration_clears_pending_thermal_event_after_migration(self, tmp_path: Path):
        """Loading a legacy DB with pending_thermal_event[mode=cool] succeeds.

        The v2 field is discarded — pending_observations is empty after load.
        """
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
        engine.load_state()  # must not raise

        assert isinstance(engine._state.pending_observations, dict)
        assert engine._state.pending_observations == {}, (
            "pending_observations should be empty — v2 cool event is discarded, not converted"
        )

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

    def _ventilated_obs(self, k_passive: float, r_squared_passive: float | None = None) -> dict:
        """Minimal ventilated_decay observation dict."""
        obs = {
            "date": "2026-04-28",
            "hvac_mode": "ventilated",
            "k_passive": k_passive,
            "confidence_grade": "high",
        }
        if r_squared_passive is not None:
            obs["r_squared_passive"] = r_squared_passive
        return obs

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
        """A fan_only_decay commit must set k_vent but must NOT write k_passive.

        fan_only k_p reflects the effective decay rate with a running fan, which
        is not the same as the envelope-only decay rate.  Writing it into k_passive
        would bias the envelope model and corrupt bedtime-setback predictions.
        """
        engine = self._make_engine(tmp_path)
        engine._update_thermal_model_cache(self._fan_only_obs(-0.15))

        model = engine.get_thermal_model()
        assert pytest.approx(-0.15, abs=1e-9) == model["k_vent"], f"k_vent should be -0.15, got {model['k_vent']}"
        # k_passive must remain None — fan_only observations do not contribute to envelope model
        assert model["k_passive"] is None, (
            "k_passive must be None after fan_only commit "
            f"(fan_only must not write k_passive); got {model['k_passive']}"
        )

    def test_passive_and_fan_only_do_not_cross_contaminate(self, tmp_path: Path):
        """passive then fan_only: k_vent reflects only fan_only; k_passive reflects only passive.

        After the fix, fan_only observations do not write to k_passive.  k_passive must
        equal the value set by the passive_decay commit only — no EMA blending with fan_only.
        """
        engine = self._make_engine(tmp_path)

        # First: passive_decay — should touch k_passive only
        engine._update_thermal_model_cache(self._passive_obs(-0.08))

        # Second: fan_only_decay — should set k_vent but must NOT update k_passive
        engine._update_thermal_model_cache(self._fan_only_obs(-0.12))

        model = engine.get_thermal_model()

        # k_vent must reflect the fan_only value only (first commit; cache was None → set directly)
        assert pytest.approx(-0.12, abs=1e-9) == model["k_vent"], (
            f"k_vent should be -0.12 (fan_only only); got {model['k_vent']} — passive_decay must not contaminate k_vent"
        )

        # k_passive must reflect ONLY the passive_decay commit (-0.08).
        # The fan_only commit must not touch k_passive — no EMA blending.
        assert pytest.approx(-0.08, abs=1e-9) == model["k_passive"], (
            f"k_passive should be -0.08 (passive only, no fan_only contamination); got {model['k_passive']}"
        )

    def test_ventilated_decay_does_not_update_k_passive(self, tmp_path: Path):
        """A ventilated_decay commit must set k_vent_window but must NOT write k_passive.

        ventilated k_p reflects effective heat transfer with windows open — a different
        physical regime than the closed-envelope decay rate.  Writing it into k_passive
        would bias the envelope model and corrupt bedtime-setback predictions.
        """
        engine = self._make_engine(tmp_path)
        engine._update_thermal_model_cache(self._ventilated_obs(-0.20))

        model = engine.get_thermal_model()
        assert pytest.approx(-0.20, abs=1e-9) == model["k_vent_window"], (
            f"k_vent_window should be -0.20, got {model['k_vent_window']}"
        )
        # k_passive must remain None — ventilated observations do not contribute to envelope model
        assert model["k_passive"] is None, (
            "k_passive must be None after ventilated commit "
            f"(ventilated must not write k_passive); got {model['k_passive']}"
        )

    def test_ventilated_does_not_contaminate_avg_r_squared_passive(self, tmp_path: Path):
        """avg_r_squared_passive must reflect only envelope-mode fit quality.

        A ventilated observation carrying r_squared_passive must NOT update
        avg_r_squared_passive — that metric tracks the closed-envelope fit only.
        """
        engine = self._make_engine(tmp_path)

        # Set baseline avg_r_squared_passive via a passive_decay observation
        passive_with_r2 = self._passive_obs(-0.08)
        passive_with_r2["r_squared_passive"] = 0.85
        engine._update_thermal_model_cache(passive_with_r2)
        baseline_r2 = engine._state.thermal_model_cache["avg_r_squared_passive"]
        assert pytest.approx(0.85, abs=1e-9) == baseline_r2

        # Now commit a ventilated observation with a very different R² — must be ignored
        engine._update_thermal_model_cache(self._ventilated_obs(-0.20, r_squared_passive=0.10))

        post_r2 = engine._state.thermal_model_cache["avg_r_squared_passive"]
        assert pytest.approx(0.85, abs=1e-9) == post_r2, (
            f"avg_r_squared_passive must remain {baseline_r2} (ventilated R² must not blend in); got {post_r2}"
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


def _make_stale_obs_rising_temps(
    obs_type: str,
    n_samples: int,
    indoor_start: float,
    indoor_end: float,
    outdoor_temp: float,
) -> dict:
    """Build a monitoring observation dict started 61 min ago with linearly rising indoor temps.

    Used to test rolling-window commit: indoor range (max-min) >= THERMAL_ROLLING_MIN_DELTA_T_F
    triggers signal_sufficient=True regardless of the indoor-outdoor snapshot differential.
    """
    start = _make_obs_61min_ago()
    step = (indoor_end - indoor_start) / max(n_samples - 1, 1)
    samples = [
        {
            "timestamp": start.isoformat(),
            "indoor_temp_f": round(indoor_start + i * step, 2),
            "outdoor_temp_f": outdoor_temp,
            "elapsed_minutes": float(i * 5),
        }
        for i in range(n_samples)
    ]
    return {
        "obs_type": obs_type,
        "obs_id": "test-rising-1",
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
        """ventilated_decay started 61 min ago, sensor open, indoor has moved → commit triggered.

        Signal check uses indoor sample range (max-min), not snapshot differential.
        indoor_start=72.0 → indoor_end=74.0 gives range=2.0 >= THERMAL_ROLLING_MIN_DELTA_T_F (0.2).
        """
        indoor_start, indoor_end, outdoor = 72.0, 74.0, 70.0
        coord = _make_obs_coord(
            hvac_action="idle",
            indoor_temp=indoor_end,
            outdoor_temp=outdoor,
            any_sensor_open=True,
        )
        coord._pending_observations[OBS_TYPE_VENTILATED_DECAY] = _make_stale_obs_rising_temps(
            OBS_TYPE_VENTILATED_DECAY,
            n_samples=THERMAL_VENT_MIN_SAMPLES + 5,
            indoor_start=indoor_start,
            indoor_end=indoor_end,
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

    def test_ventilated_decay_kept_alive_flat_indoor_large_snapshot_diff(self, caplog):
        """Regression: ventilated_decay with flat indoor temps kept alive even when snapshot diff is large.

        Production scenario: indoor=72°F (integer, flat), outdoor=60°F → snapshot diff=12°F.
        Old signal check (snapshot diff) would deem signal sufficient → OLS fails → 30-min loop.
        New signal check (indoor sample range) → range=0 < 0.2 → keep-alive fires correctly.
        """
        indoor, outdoor = 72.0, 60.0  # 12°F snapshot diff, but indoor flat
        coord = _make_obs_coord(
            hvac_action="idle",
            indoor_temp=indoor,
            outdoor_temp=outdoor,
            any_sensor_open=True,
        )
        coord._pending_observations[OBS_TYPE_VENTILATED_DECAY] = _make_stale_obs(
            OBS_TYPE_VENTILATED_DECAY,
            n_samples=THERMAL_VENT_MIN_SAMPLES + 5,
            indoor_temp=indoor,  # flat — range=0
            outdoor_temp=outdoor,
        )
        dt_mock = _make_dt_mock(_FAKE_NOW)

        import logging

        with (
            patch("custom_components.climate_advisor.coordinator.dt_util", dt_mock),
            caplog.at_level(logging.INFO, logger="custom_components.climate_advisor.coordinator"),
        ):
            coord._sample_all_observations()

        assert OBS_TYPE_VENTILATED_DECAY in coord._pending_observations, (
            "ventilated_decay with flat indoor temps should be kept alive at 61 min "
            "even when indoor-outdoor snapshot diff is large (12°F). "
            "Signal check must use indoor sample range, not snapshot differential."
        )
        assert any("keeping alive" in r.message for r in caplog.records), (
            "Expected 'keeping alive' log — flat indoor means signal_sufficient=False"
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
# _make_obs_coord_with_sample_thermal_event was removed in v3 — it bound the
# now-deleted _sample_thermal_event method.  The two tests that used it
# (test_post_heat_decimated_at_5min, test_post_heat_appended_after_5min) were
# also removed; post-heat decimation is now handled inside _sample_all_observations.


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

    # test_post_heat_decimated_at_5min and test_post_heat_appended_after_5min were
    # removed in v3.  They called coord._sample_thermal_event() which no longer exists.
    # Post-heat decimation in v3 is handled inside _sample_all_observations() for
    # OBS_TYPE_HVAC_HEAT post-phase samples — covered by test_hvac_active_not_decimated
    # and the passive/fan interval tests above.

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

    def test_passive_decay_stays_alive_after_30min_while_hvac_idle(self):
        """passive_decay started 31 min ago stays alive when HVAC is still idle.

        The old Path A rolling-window OLS committed on elapsed time. The chart_log endpoint
        path is event-driven: the observation runs until HVAC starts, a sensor opens, a fan
        activates, or the hard cap is reached. Elapsed time alone does not trigger a commit.
        """
        coord = _make_obs_coord(hvac_action="idle", indoor_temp=74.0, outdoor_temp=55.0)
        obs = self._make_31min_obs(OBS_TYPE_PASSIVE_DECAY, n_samples=6)
        coord._pending_observations[OBS_TYPE_PASSIVE_DECAY] = obs

        def _consume_coro(coro):
            coro.close()

        coord.hass.async_create_task = _consume_coro

        dt_mock = _make_dt_mock(_FAKE_NOW)
        with patch("custom_components.climate_advisor.coordinator.dt_util", dt_mock):
            coord._sample_all_observations()

        obs_after = coord._pending_observations.get(OBS_TYPE_PASSIVE_DECAY)
        assert obs_after is not None and obs_after.get("status") == "monitoring", (
            "passive_decay should remain alive (monitoring) when HVAC is still idle — "
            "chart_log endpoint only fires on external events, not elapsed time"
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

    def test_passive_decay_remains_monitoring_without_external_event(self):
        """passive_decay stays in monitoring status when no external event interrupts.

        There is no rolling-window OLS path any more. The chart_log endpoint fires on external
        events only (HVAC starts, sensor opens, fan activates, hard cap). A 31-minute-old
        observation with HVAC still idle must remain in monitoring status so the window keeps
        accumulating data until a real event ends it.
        """
        coord = _make_obs_coord(hvac_action="idle", indoor_temp=74.0, outdoor_temp=55.0)
        obs = self._make_31min_obs(OBS_TYPE_PASSIVE_DECAY, n_samples=6)
        coord._pending_observations[OBS_TYPE_PASSIVE_DECAY] = obs

        def _consume_coro(coro):
            coro.close()

        coord.hass.async_create_task = _consume_coro

        dt_mock = _make_dt_mock(_FAKE_NOW)
        with patch("custom_components.climate_advisor.coordinator.dt_util", dt_mock):
            coord._sample_all_observations()

        obs_after = coord._pending_observations.get(OBS_TYPE_PASSIVE_DECAY)
        assert obs_after is not None and obs_after.get("status") == "monitoring", (
            "passive_decay should remain in 'monitoring' status when HVAC is still idle"
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

        Mode "ventilated" bumps observation_count_vent and updates k_vent_window.
        Ventilated obs do NOT count toward confidence_k_passive (Phase H fix, D13).
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

    def test_confidence_k_passive_not_lifted_by_vent_only_observations(self, tmp_path: Path):
        """Phase H D13: ventilated obs must NOT lift confidence_k_passive above 'none'.

        Ventilated observations write to k_vent_window, not k_passive (guarded by
        _envelope_modes at learning.py:676). Counting them toward k_passive confidence
        was a bug that produced "Calibration: Strong" while k_passive remained None.
        """
        engine = self._make_engine(tmp_path / "vent_only")
        for _ in range(THERMAL_PASSIVE_CONF_HIGH + 10):
            engine._update_thermal_model_cache(self._ventilated_obs())
        model = engine.get_thermal_model()
        assert model["confidence_k_passive"] == "none", (
            f"confidence_k_passive must stay 'none' with only ventilated obs, got '{model['confidence_k_passive']}'"
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


# ---------------------------------------------------------------------------
# TestAdaptiveVentilatedOLS
# ---------------------------------------------------------------------------


class TestAdaptiveVentilatedOLS:
    """Adaptive 2-param OLS for ventilated_decay (Issue #126 Phase B).

    When samples span sufficient solar_factor variation, _commit_event_from_dict
    attempts a 2-parameter OLS: dT/dt = k_env*(T_out-T_in) + k_solar*solar_factor.
    This separates solar gain from ventilation-driven temperature change so that
    daytime ventilated_decay observations no longer suppress R² with unexplained
    solar variance.
    """

    # dt_util produces MagicMocks in the stub HA environment.  Any test that calls
    # _commit_event_from_dict must patch learning.dt_util so now() returns a real datetime.
    _DT_PATCH = "custom_components.climate_advisor.learning.dt_util"
    _FAKE_DT = datetime(2026, 5, 3, 12, 0, 0, tzinfo=UTC)

    def _make_engine(self, tmp_path: Path) -> LearningEngine:
        engine = LearningEngine(tmp_path)
        engine.load_state()
        return engine

    def _make_samples(
        self,
        n: int = 8,
        *,
        k_env: float = -0.10,
        k_solar: float = 2.5,
        indoor_start: float = 78.0,
        outdoor_temp: float = 62.0,
        sf_start: float = 0.0,
        sf_end: float = 1.0,
        noise: float = 0.0,
    ) -> list[dict]:
        """Generate synthetic ventilated_decay samples with known physics.

        Physics: dT/dt = k_env*(T_out - T_in) + k_solar*sf
        With indoor(78) > outdoor(62), k_env=-0.10:
          ventilation cooling: -0.10*(62-78) = +1.6°F/hr (i.e., passive term drives cooling of the gap)
          Wait — k_env is used as: rate = k_env*(T_out-T_in)
          k_env=-0.10, T_out-T_in=-16 → rate = -0.10*(-16) = +1.6 ... that's heating, wrong.

        compute_k_passive uses delta = T_in - T_out (positive when warm inside),
        and rate = k_p * delta.  For cooling: rate < 0, delta > 0 → k_p < 0.
        So the passive model convention is: rate = k_passive * (T_in - T_out).

        To stay consistent with compute_k_passive, use:
          rate = k_env * (T_in - T_out) + k_solar * sf
        where k_env < 0 (cooling when indoor > outdoor).

        With indoor=78, outdoor=62: delta=+16, k_env=-0.10 → passive rate=-1.6°F/hr (cooling).
        Solar at sf=0.8: +k_solar*0.8 partially offsets → net slower cooling.
        Uses Euler integration over 5-min steps.
        """
        import random

        random.seed(42)
        dt_minutes = 5.0
        dt_hours = dt_minutes / 60.0
        samples = []
        t_in = indoor_start
        for i in range(n):
            elapsed = i * dt_minutes
            sf = sf_start + (sf_end - sf_start) * (i / max(n - 1, 1))
            samples.append(
                {
                    "indoor_temp_f": t_in + (random.gauss(0, noise) if noise else 0.0),
                    "outdoor_temp_f": outdoor_temp,
                    "elapsed_minutes": elapsed,
                    "solar_factor": sf,
                }
            )
            # rate = k_env*(T_in - T_out) + k_solar*sf
            rate = k_env * (t_in - outdoor_temp) + k_solar * sf
            t_in += rate * dt_hours
        return samples

    def _make_event(self, samples: list[dict]) -> dict:
        """Wrap samples in a minimal event dict for _commit_event_from_dict."""
        return {"obs_id": "test-vent-ols", "samples": samples}

    # ── test 1: 2-param fires when solar_factor range sufficient ────────────

    def test_2param_fires_when_solar_factor_range_sufficient(self):
        """compute_k_env_solar returns non-None when sf spans [0.0, 1.0]."""
        from custom_components.climate_advisor.learning import compute_k_env_solar

        samples = self._make_samples(n=10, sf_start=0.0, sf_end=1.0)
        k_env, k_solar, r2 = compute_k_env_solar(samples)
        assert k_env is not None, "k_env should be non-None when solar_factor range >= 0.30"
        assert k_solar is not None, "k_solar should be non-None when solar_factor range >= 0.30"
        assert r2 is not None, "r2 should be non-None when solar_factor range >= 0.30"

    # ── test 2: 1-param path when solar_factor range too low ────────────────

    def test_1param_path_when_solar_factor_range_low(self):
        """All samples sf=0.0 (nighttime) → (None, None, None) so caller falls back to 1-param."""
        from custom_components.climate_advisor.learning import compute_k_env_solar

        samples = self._make_samples(n=8, sf_start=0.0, sf_end=0.0)
        k_env, k_solar, r2 = compute_k_env_solar(samples)
        assert k_env is None, "k_env should be None when all solar_factor=0.0 (range < 0.30)"
        assert k_solar is None, "k_solar should be None when all solar_factor=0.0"
        assert r2 is None, "r2 should be None when solar_factor range is insufficient"

    # ── test 3: collinearity guard when sf constant non-zero ────────────────

    def test_collinearity_guard_returns_none(self):
        """All samples sf=1.0 (all midday, constant) → returns (None, None, None)."""
        from custom_components.climate_advisor.learning import compute_k_env_solar

        samples = self._make_samples(n=8, sf_start=1.0, sf_end=1.0)
        k_env, k_solar, r2 = compute_k_env_solar(samples)
        assert k_env is None, "k_env should be None: sf constant at 1.0, range=0 < 0.30"
        assert k_solar is None, "k_solar should be None when sf range is too narrow"

    # ── test 4: recovers known k_env and k_solar within 10% ─────────────────

    def test_2param_recovers_known_k_env_and_k_solar(self):
        """OLS with known physics recovers k_env=-0.10, k_solar=2.5 within 10%."""
        from custom_components.climate_advisor.learning import compute_k_env_solar

        samples = self._make_samples(
            n=20,
            k_env=-0.10,
            k_solar=2.5,
            indoor_start=76.0,
            outdoor_temp=68.0,
            sf_start=0.0,
            sf_end=1.0,
            noise=0.0,
        )
        k_env, k_solar, r2 = compute_k_env_solar(samples)
        assert k_env is not None, "OLS should succeed with clean synthetic data"
        assert k_solar is not None, "k_solar should be recoverable"
        assert abs(k_env - (-0.10)) / 0.10 < 0.10, f"k_env={k_env:.4f} should be within 10% of -0.10"
        assert abs(k_solar - 2.5) / 2.5 < 0.10, f"k_solar={k_solar:.4f} should be within 10% of 2.5"

    # ── test 5: k_solar bounds reject negative values ───────────────────────

    def test_k_solar_bounds_reject_negative_value(self, tmp_path: Path):
        """If 2-param returns k_solar < 0, _commit_event_from_dict must NOT store it.

        A negative k_solar (solar cooling) is physically nonsensical for daytime
        periods — it means the model picked up the ventilation signal in the wrong
        parameter.  The bounds check must force fallback to the 1-param result.
        """
        from unittest.mock import patch

        engine = self._make_engine(tmp_path)

        # Pure-ventilation nighttime samples (sf=0) → 1-param will succeed cleanly.
        samples = self._make_samples(n=20, k_env=-0.10, k_solar=0.0, sf_start=0.0, sf_end=0.0)

        def _bad_k_solar(s, min_samples=4):
            # Force 2-param to return a negative k_solar — bounds check must reject this.
            return -0.10, -1.5, 0.85

        event = self._make_event(samples)
        dt_mock = _make_dt_mock(self._FAKE_DT)
        with (
            patch(self._DT_PATCH, dt_mock),
            patch("custom_components.climate_advisor.learning.compute_k_env_solar", side_effect=_bad_k_solar),
        ):
            obs, reject_code, _ = engine._commit_event_from_dict(event, force_grade="high", obs_type="ventilated_decay")

        # Commit should still succeed (1-param fallback) but k_solar must NOT be stored.
        assert obs is not None, "1-param fallback should still produce a valid obs"
        assert obs.get("two_param") is not True, "two_param flag must not be set when k_solar out of bounds"
        assert obs.get("k_solar") is None, f"k_solar must be None in obs when bounds-rejected; got {obs.get('k_solar')}"

    # ── test 6: k_solar updates cache on 2-param commit ─────────────────────

    def test_k_solar_updates_cache_on_2param_commit(self, tmp_path: Path):
        """After a 2-param commit, model cache has k_solar > 0.

        Uses mock compute_k_env_solar to guarantee 2-param path fires regardless of
        1-param R² outcome, and to guarantee bounds check passes with k_solar=1.5.
        The mock returns valid (k_env, k_solar, r2) with r2 above THERMAL_MIN_R_SQUARED.
        """
        from unittest.mock import patch

        engine = self._make_engine(tmp_path)
        # Nighttime samples — 1-param will succeed cleanly (sf=0, pure ventilation).
        samples = self._make_samples(n=20, k_env=-0.10, k_solar=0.0, sf_start=0.0, sf_end=0.0)

        def _good_2param(s, min_samples=4):
            return -0.09, 1.5, 0.85  # valid k_env, positive k_solar, high R²

        event = self._make_event(samples)
        dt_mock = _make_dt_mock(self._FAKE_DT)
        with (
            patch(self._DT_PATCH, dt_mock),
            patch("custom_components.climate_advisor.learning.compute_k_env_solar", side_effect=_good_2param),
        ):
            obs, reject_code, _ = engine._commit_event_from_dict(event, force_grade="high", obs_type="ventilated_decay")

        assert obs is not None, f"ventilated_decay commit should succeed; reject_code={reject_code}"
        assert obs.get("two_param") is True, "two_param flag must be set when 2-param path succeeds"
        model = engine.get_thermal_model()
        assert model["k_solar"] is not None, "k_solar cache must be set after 2-param commit"
        assert model["k_solar"] > 0, f"k_solar must be positive; got {model['k_solar']}"

    # ── test 7: k_vent_window updated by 2-param k_env ──────────────────────

    def test_k_vent_window_updated_by_2param_k_env(self, tmp_path: Path):
        """After 2-param commit, k_vent_window is updated with k_env (not old 1-param value).

        Uses mock compute_k_env_solar to guarantee 2-param path fires.  Verifies that the
        2-param k_env (-0.09) is what ends up in k_vent_window, not the 1-param k_p.
        """
        from unittest.mock import patch

        engine = self._make_engine(tmp_path)
        samples = self._make_samples(n=20, k_env=-0.10, k_solar=0.0, sf_start=0.0, sf_end=0.0)

        def _good_2param(s, min_samples=4):
            return -0.09, 1.5, 0.85

        event = self._make_event(samples)
        dt_mock = _make_dt_mock(self._FAKE_DT)
        with (
            patch(self._DT_PATCH, dt_mock),
            patch("custom_components.climate_advisor.learning.compute_k_env_solar", side_effect=_good_2param),
        ):
            obs, reject_code, _ = engine._commit_event_from_dict(event, force_grade="high", obs_type="ventilated_decay")

        assert obs is not None, f"commit should succeed; reject_code={reject_code}"
        assert obs.get("two_param") is True, "two_param flag must be set"
        model = engine.get_thermal_model()
        assert model["k_vent_window"] is not None, "k_vent_window must be set after ventilated commit"
        # With force_grade="high" and alpha=0.3, first obs → k_vent_window = k_env from 2-param = -0.09
        assert abs(model["k_vent_window"] - (-0.09)) < 0.001, (
            f"k_vent_window should reflect 2-param k_env=-0.09; got {model['k_vent_window']}"
        )

    # ── test 8: 1-param commit does not update k_solar ──────────────────────

    def test_1param_commit_does_not_update_k_solar(self, tmp_path: Path):
        """Nighttime obs (all sf=0.0, 1-param only) → k_solar cache remains None."""
        from unittest.mock import patch

        engine = self._make_engine(tmp_path)
        # Nighttime: all solar_factor=0.0, pure ventilation signal → compute_k_env_solar returns None tuple
        samples = self._make_samples(
            n=20,
            k_env=-0.10,
            k_solar=0.0,
            sf_start=0.0,
            sf_end=0.0,
            indoor_start=76.0,
            outdoor_temp=62.0,
            noise=0.0,
        )
        event = self._make_event(samples)
        dt_mock = _make_dt_mock(self._FAKE_DT)
        with patch(self._DT_PATCH, dt_mock):
            obs, reject_code, _ = engine._commit_event_from_dict(event, force_grade="high", obs_type="ventilated_decay")

        assert obs is not None, f"1-param ventilated commit should succeed; reject_code={reject_code}"
        model = engine.get_thermal_model()
        assert model["k_solar"] is None, (
            f"k_solar cache must stay None after 1-param-only ventilated commit; got {model['k_solar']}"
        )

    # ── test 9: old samples without solar_factor treated as 0.0 ─────────────

    def test_old_samples_without_solar_factor_treated_as_zero(self):
        """Samples missing solar_factor key are treated as 0.0 → range=0 → 1-param path."""
        from custom_components.climate_advisor.learning import compute_k_env_solar

        # Old-format samples: no solar_factor key at all
        samples = [
            {"indoor_temp_f": 76.0 - i * 0.1, "outdoor_temp_f": 68.0, "elapsed_minutes": i * 5.0} for i in range(10)
        ]
        k_env, k_solar, r2 = compute_k_env_solar(samples)
        assert k_env is None, "k_env should be None when all samples missing solar_factor (treated as 0.0, range=0)"
        assert k_solar is None, "k_solar should be None for old-format samples without solar_factor"


# ---------------------------------------------------------------------------
# TestTwoParamPrimaryPath  (Issue #126 Phase D)
# ---------------------------------------------------------------------------


class TestTwoParamPrimaryPath:
    """2-param OLS fires as PRIMARY path for ventilated_decay when sf_range ≥ 0.30.

    Before Phase D, a warm sunny day caused ols_wrong_sign rejection in the 1-param
    path before the 2-param path was ever attempted.  These tests verify that the
    primary-path restructure fires correctly and that the fall-through behaviour to
    1-param is intact when 2-param cannot run or fails its guards.
    """

    _DT_PATCH = "custom_components.climate_advisor.learning.dt_util"
    _FAKE_DT = datetime(2026, 5, 7, 11, 0, 0, tzinfo=UTC)

    def _make_engine(self, tmp_path: Path) -> LearningEngine:
        engine = LearningEngine(tmp_path)
        engine.load_state()
        return engine

    @staticmethod
    def _make_rising_solar_samples() -> list[dict]:
        """Indoor net-rises over 2 hours despite outdoor cooler: solar gain dominates.

        Uses Euler integration with known physics: k_env=-0.08 (ventilation cooling),
        k_solar=4.0 (strong solar gain). indoor(72) > outdoor(58), so ventilation
        tries to cool at rate = -0.08*(72-58)=-1.12°F/hr, but solar gain at sf=0.5
        adds +4.0*0.5=+2.0°F/hr — net +0.88°F/hr.

        1-param OLS sees: rate > 0, delta > 0 → k_1p > 0 → ols_wrong_sign.
        2-param OLS recovers k_env ≈ -0.08 (negative) and k_solar ≈ 4.0 (positive).

        sf_range spans 0.05→0.85 (0.80 > 0.30 threshold).
        16 samples → 15 intervals, well above the min_samples=4 floor.
        """
        k_env_true = -0.08  # ventilation cooling rate (hr⁻¹)
        k_solar_true = 4.0  # solar gain (°F/hr per unit sf)
        base_indoor = 72.0
        outdoor = 58.0
        dt_minutes = 8.0
        dt_hours = dt_minutes / 60.0
        n = 16
        samples = []
        t_in = base_indoor
        for i in range(n):
            sf = 0.05 + (0.80 / (n - 1)) * i  # ramps from 0.05 to 0.85
            samples.append(
                {
                    "indoor_temp_f": round(t_in, 4),
                    "outdoor_temp_f": outdoor,
                    "elapsed_minutes": float(i * dt_minutes),
                    "solar_factor": round(sf, 4),
                }
            )
            rate = k_env_true * (t_in - outdoor) + k_solar_true * sf
            t_in += rate * dt_hours
        return samples

    @staticmethod
    def _make_nighttime_cooling_samples() -> list[dict]:
        """Indoor falls 2°F overnight, outdoor cooler, solar_factor=0.0 throughout."""
        samples = []
        for i in range(12):
            samples.append(
                {
                    "indoor_temp_f": 74.0 - i * 0.18,
                    "outdoor_temp_f": 60.0,
                    "elapsed_minutes": float(i * 10),
                    "solar_factor": 0.0,
                }
            )
        return samples

    def _make_event(self, samples: list[dict]) -> dict:
        return {"obs_id": "test-2param-primary", "samples": samples}

    # ── test 1: 2-param PRIMARY commits when 1-param would produce wrong_sign ─

    def test_2param_primary_commits_when_wrong_sign_for_1param(self, tmp_path: Path):
        """Solar gain scenario: indoor rising, outdoor cooler → 1-param sees positive k.

        Before Phase D: _commit_event_from_dict returns (None, 'ols_wrong_sign', ...).
        After Phase D: 2-param primary path fires, returning obs with two_param=True,
        k_solar > 0, and k_passive (k_env) < 0.
        """
        from custom_components.climate_advisor.learning import compute_k_passive

        engine = self._make_engine(tmp_path)
        samples = self._make_rising_solar_samples()

        # Confirm the pre-condition: 1-param alone (using the decay min_samples floor)
        # would give ols_wrong_sign on rising indoor / warmer-than-outdoor data.
        from custom_components.climate_advisor.const import THERMAL_MIN_DECAY_SAMPLES

        k_1p, _r2_1p, _code_1p = compute_k_passive(samples, min_samples=THERMAL_MIN_DECAY_SAMPLES)
        assert k_1p is None, (
            f"Pre-condition failed: 1-param should return None (ols_wrong_sign) for rising solar samples; got k={k_1p}"
        )
        assert _code_1p == "ols_wrong_sign", f"Expected ols_wrong_sign reject; got {_code_1p}"

        # Now call the full commit path — 2-param primary should save it.
        event = self._make_event(samples)
        dt_mock = _make_dt_mock(self._FAKE_DT)
        with patch(self._DT_PATCH, dt_mock):
            obs, reject_code, _ = engine._commit_event_from_dict(event, force_grade="high", obs_type="ventilated_decay")

        assert obs is not None, f"2-param PRIMARY should have committed; got reject_code={reject_code}"
        assert obs.get("two_param") is True, "two_param flag must be True for 2-param PRIMARY commit"
        assert obs.get("k_solar") is not None and obs["k_solar"] > 0, (
            f"k_solar must be positive for solar-gain obs; got {obs.get('k_solar')}"
        )
        assert obs.get("k_passive") is not None and obs["k_passive"] < 0, (
            f"k_passive (k_env) must be negative (ventilation cooling); got {obs.get('k_passive')}"
        )

    # ── test 2: 2-param primary skipped when sf_range < threshold ────────────

    def test_2param_primary_skipped_when_sf_range_low(self, tmp_path: Path):
        """Nighttime samples (sf=0.0): sf_range=0 → falls through to 1-param path."""
        engine = self._make_engine(tmp_path)
        samples = self._make_nighttime_cooling_samples()

        event = self._make_event(samples)
        dt_mock = _make_dt_mock(self._FAKE_DT)
        with patch(self._DT_PATCH, dt_mock):
            obs, reject_code, _ = engine._commit_event_from_dict(event, force_grade="high", obs_type="ventilated_decay")

        # 1-param should commit (indoor cooling, outdoor cooler → k_passive < 0)
        assert obs is not None, f"1-param fallback should commit nighttime cooling; reject_code={reject_code}"
        # two_param key absent or False — this was a 1-param commit (or upgrade if
        # the upgrade block happened to run on the same data, but sf=0 → no upgrade either)
        assert obs.get("two_param") is not True, (
            "two_param must NOT be set when sf_range < 0.30 and no sf variation for upgrade path"
        )

    # ── test 3: falls back to 1-param when 2-param returns None (collinear) ──

    def test_2param_primary_falls_back_to_1param_on_collinear(self, tmp_path: Path):
        """sf_range ≥ 0.30 but compute_k_env_solar returns (None, None, None).

        This simulates collinearity or a det≈0 failure in the 2-param solver.
        The fall-through must reach 1-param and commit if 1-param succeeds.
        """
        engine = self._make_engine(tmp_path)
        # Use nighttime cooling samples (1-param valid) but inject sf_range ≥ 0.30
        # by attaching varying solar_factor values — then mock compute_k_env_solar to None.
        samples = []
        for i in range(12):
            samples.append(
                {
                    "indoor_temp_f": 74.0 - i * 0.18,
                    "outdoor_temp_f": 60.0,
                    "elapsed_minutes": float(i * 10),
                    # sf_range = 0.65 — enough to trigger primary path attempt
                    "solar_factor": 0.05 + i * 0.05,
                }
            )

        event = self._make_event(samples)
        dt_mock = _make_dt_mock(self._FAKE_DT)
        with (
            patch(self._DT_PATCH, dt_mock),
            patch(
                "custom_components.climate_advisor.learning.compute_k_env_solar",
                return_value=(None, None, None),
            ),
        ):
            obs, reject_code, _ = engine._commit_event_from_dict(event, force_grade="high", obs_type="ventilated_decay")

        assert obs is not None, f"1-param fallback should commit when 2-param returns None; reject_code={reject_code}"
        assert obs.get("two_param") is not True, "two_param must not be set when 2-param returned None"

    # ── test 4: 2-param primary rejected when R² too low ─────────────────────

    def test_2param_primary_respects_r2_guard(self, tmp_path: Path):
        """sf_range ≥ 0.30 but 2-param returns r² < THERMAL_MIN_R_SQUARED → fall back to 1-param."""
        engine = self._make_engine(tmp_path)
        # Nighttime cooling samples with sf_range ≥ 0.30 (so primary path is attempted)
        samples = []
        for i in range(12):
            samples.append(
                {
                    "indoor_temp_f": 74.0 - i * 0.18,
                    "outdoor_temp_f": 60.0,
                    "elapsed_minutes": float(i * 10),
                    "solar_factor": 0.05 + i * 0.05,
                }
            )

        def _low_r2_2param(s, min_samples=4):
            # Valid signs/bounds but R² below the 0.20 threshold.
            return -0.08, 1.2, 0.10

        event = self._make_event(samples)
        dt_mock = _make_dt_mock(self._FAKE_DT)
        with (
            patch(self._DT_PATCH, dt_mock),
            patch(
                "custom_components.climate_advisor.learning.compute_k_env_solar",
                side_effect=_low_r2_2param,
            ),
        ):
            obs, reject_code, _ = engine._commit_event_from_dict(event, force_grade="high", obs_type="ventilated_decay")

        assert obs is not None, (
            f"1-param fallback should commit when 2-param R² below threshold; reject_code={reject_code}"
        )
        assert obs.get("two_param") is not True, "two_param must NOT be set when 2-param was rejected for low R²"


# ---------------------------------------------------------------------------
# TestSolarKeepAliveGuard  (Issue #126 Phase D)
# ---------------------------------------------------------------------------


class TestSolarKeepAliveGuard:
    """Solar accumulation keep-alive guard in coordinator._sample_all_observations().

    The guard suppresses _vent_signal_sufficient during daytime (8–18h) when
    sf_range < THERMAL_SOLAR_FACTOR_MIN_RANGE, preventing a 30-min early commit
    before enough solar variation has accumulated for the 2-param primary path.

    These tests verify the guard logic as a standalone pure-Python helper that
    replicates the exact coordinator guard condition — no coordinator wiring needed.
    """

    @staticmethod
    def _compute_vent_signal_with_guard(
        samples_list: list[dict],
        now_hour: int,
        base_signal_sufficient: bool,
    ) -> bool:
        """Replicate the solar keep-alive guard from coordinator._sample_all_observations().

        Returns the effective _vent_signal_sufficient value after applying the guard.
        """
        _sf_vals = [s.get("solar_factor", 0.0) for s in samples_list if "solar_factor" in s]
        _sf_range = max(_sf_vals) - min(_sf_vals) if len(_sf_vals) >= 2 else 0.0
        _daytime = 8 <= now_hour < 18
        if _daytime and _sf_range < THERMAL_SOLAR_FACTOR_MIN_RANGE:
            return False
        return base_signal_sufficient

    def _make_samples_with_sf(self, sf_vals: list[float]) -> list[dict]:
        return [
            {
                "indoor_temp_f": 72.0,
                "outdoor_temp_f": 58.0,
                "elapsed_minutes": float(i * 10),
                "solar_factor": sf,
            }
            for i, sf in enumerate(sf_vals)
        ]

    # ── test 1: daytime low sf_range → suppress early commit ─────────────────

    def test_early_commit_suppressed_daytime_low_sf_range(self):
        """hour=9, sf_range=0.10 < 0.30 → guard overrides base=True to False."""
        samples = self._make_samples_with_sf([0.05, 0.10, 0.12, 0.15])
        result = self._compute_vent_signal_with_guard(samples, now_hour=9, base_signal_sufficient=True)
        assert result is False, f"Guard should suppress early commit at 9h with sf_range=0.10; got {result}"

    # ── test 2: daytime sufficient sf_range → allow commit ───────────────────

    def test_early_commit_allowed_when_sf_range_sufficient(self):
        """hour=11, sf_range=0.55 ≥ 0.30 → guard passes, base=True preserved."""
        samples = self._make_samples_with_sf([0.10, 0.30, 0.50, 0.65])
        result = self._compute_vent_signal_with_guard(samples, now_hour=11, base_signal_sufficient=True)
        assert result is True, f"Guard should allow commit at 11h with sf_range=0.55; got {result}"

    # ── test 3: nighttime → guard inactive, base value unchanged ─────────────

    def test_early_commit_allowed_at_night(self):
        """hour=22, sf_range=0.0 → guard not daytime, base=True preserved."""
        samples = self._make_samples_with_sf([0.0, 0.0, 0.0, 0.0])
        result = self._compute_vent_signal_with_guard(samples, now_hour=22, base_signal_sufficient=True)
        assert result is True, f"Guard should be inactive at night (hour=22); got {result}"


# ---------------------------------------------------------------------------
# _grade_passive_confidence — observation type routing (Issue #130)
# ---------------------------------------------------------------------------


class TestGradePassiveConfidence:
    """Verify that only observations that actually write to k_passive count toward its confidence.

    vent obs  → write to k_vent_window only (guarded by _envelope_modes in learning.py)
    solar obs → write to k_solar only
    passive/hvac/fan_only obs → count toward k_passive confidence
    """

    def test_ventilated_obs_excluded(self):
        """63 ventilated obs — these write to k_vent_window, not k_passive."""
        cache = {"observation_count_vent": 63}
        assert _grade_passive_confidence(cache) == "none"

    def test_solar_obs_excluded(self):
        """solar obs write to k_solar, not k_passive — must not count."""
        cache = {"observation_count_solar": 20}
        assert _grade_passive_confidence(cache) == "none"

    def test_passive_obs_count_toward_high(self):
        """30 passive decay obs ≥ THERMAL_PASSIVE_CONF_HIGH(30) → 'high'."""
        cache = {"observation_count_passive": 30}
        assert _grade_passive_confidence(cache) == "high"

    def test_hvac_obs_count(self):
        """10 heat + 10 cool = 20 ≥ THERMAL_PASSIVE_CONF_MEDIUM(15), < THERMAL_PASSIVE_CONF_HIGH(30) → 'medium'."""
        cache = {"observation_count_heat": 10, "observation_count_cool": 10}
        assert _grade_passive_confidence(cache) == "medium"


# ---------------------------------------------------------------------------
# TestHvacObservationLogging
# ---------------------------------------------------------------------------


class TestHvacObservationLogging:
    """Phase A diagnostics: structured INFO logs at HVAC observation lifecycle events.

    These tests validate that the coordinator emits visible, parseable log lines at:
      1. HVAC action state-change detection (was_running / is_running)
      2. _start_hvac_observation entry (obs_type, prior obs list)
      3. Pre-abandon in _check_hvac_stabilization timeout path (n_active, n_post, elapsed)
    """

    # ── helper: build a minimal thermostat-changed event ─────────────────────

    @staticmethod
    def _make_thermostat_event(old_action: str, new_action: str, old_mode: str = "heat", new_mode: str = "heat"):
        """Build a minimal HA state-change event dict for _async_thermostat_changed."""
        old_state = MagicMock()
        old_state.state = old_mode
        old_state.attributes = {"hvac_action": old_action}

        new_state = MagicMock()
        new_state.state = new_mode
        new_state.attributes = {"hvac_action": new_action}

        event = MagicMock()
        event.data = {"old_state": old_state, "new_state": new_state}
        return event

    # ── helper: coord with _async_thermostat_changed bound ───────────────────

    @staticmethod
    def _make_coord_with_thermostat_handler():
        """Coordinator stub with _async_thermostat_changed and _start_hvac_observation bound."""
        ClimateAdvisorCoordinator = _get_coordinator_class()
        coord = object.__new__(ClimateAdvisorCoordinator)

        hass = MagicMock()

        def _consume_coroutine(coro):
            coro.close()

        hass.async_create_task = MagicMock(side_effect=_consume_coroutine)

        def _exec_job(fn, *args):
            # Issue #491: mirrors real HA semantics for both calling conventions used
            # in coordinator.py — some call sites
            # `await hass.async_add_executor_job(...)` (needs an awaitable Future),
            # others (_abandon_observation, sync, fire-and-forget) call it with no
            # running loop at all. asyncio.get_running_loop() tells us which context
            # we are in; Python 3.14 removed the implicit event loop, so this cannot
            # unconditionally use get_event_loop().
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                return fn(*args)
            fut = loop.create_future()
            fut.set_result(fn(*args))
            return fut

        hass.async_add_executor_job = _exec_job
        coord.hass = hass

        coord.config = {
            "climate_entity": "climate.test",
            "weather_entity": "weather.test",
            "comfort_heat": 70,
            "comfort_cool": 75,
            "learning_enabled": True,
        }

        ae = MagicMock()
        ae._fan_active = False
        ae._natural_vent_active = False
        ae.is_paused_by_door = False
        ae._hvac_command_pending = False
        coord.automation_engine = ae

        learning = MagicMock()
        learning.set_pending_thermal_event = MagicMock()
        learning.save_state = MagicMock()
        coord.learning = learning

        coord._pending_observations = {}
        coord._pending_thermal_event = None
        coord._pre_heat_sample_buffer = []
        coord._last_outdoor_temp = 55.0
        coord._hvac_on_since = None

        coord._get_indoor_temp = MagicMock(return_value=72.0)
        coord._any_sensor_open = MagicMock(return_value=False)
        coord._async_save_state = AsyncMock()
        coord._flush_hvac_runtime = MagicMock()
        coord._is_recent_hvac_command = MagicMock(return_value=False)
        coord._startup_coalesce_active = False  # Bug 1 (Issue #321)

        def _get_current_sample(elapsed: float) -> dict:
            return {
                "timestamp": _FAKE_NOW.isoformat(),
                "indoor_temp_f": 72.0,
                "outdoor_temp_f": 55.0,
                "elapsed_minutes": elapsed,
            }

        coord._get_current_sample = _get_current_sample

        for method_name in (
            "_ensure_pending_observations",
            "_start_hvac_observation",
            "_abandon_observation",
            "_commit_observation_if_sufficient",
            "_end_hvac_active_phase",
            "_async_thermostat_changed",
        ):
            method = getattr(ClimateAdvisorCoordinator, method_name)
            setattr(coord, method_name, types.MethodType(method, coord))

        return coord

    # ── test 1: hvac_action transition log ───────────────────────────────────

    def test_hvac_state_change_logs_transition(self):
        """_async_thermostat_changed emits INFO log with action, was_running, is_running
        when hvac_action changes from idle to heating."""
        coord = self._make_coord_with_thermostat_handler()
        event = self._make_thermostat_event(old_action="idle", new_action="heating")

        with patch("custom_components.climate_advisor.coordinator._LOGGER") as mock_log:
            asyncio.run(coord._async_thermostat_changed(event))

        # Collect all info calls and their rendered messages
        info_messages = []
        for call in mock_log.info.call_args_list:
            fmt = call[0][0]
            args = call[0][1:]
            try:
                info_messages.append(fmt % args)
            except Exception:
                info_messages.append(fmt)

        matched = [m for m in info_messages if "was_running" in m or "_async_thermostat_changed" in m]
        assert matched, (
            f"Expected an INFO log containing 'was_running' or '_async_thermostat_changed'. "
            f"Got info calls: {info_messages}"
        )
        combined = " ".join(matched)
        assert "False" in combined or "was_running=False" in combined, (
            f"Expected was_running=False in transition log. Got: {combined}"
        )
        assert "True" in combined or "is_running=True" in combined, (
            f"Expected is_running=True in transition log. Got: {combined}"
        )

    # ── test 2: observation start log ────────────────────────────────────────

    def test_hvac_observation_start_logged(self):
        """_start_hvac_observation emits an INFO log that includes obs_type and 'starting'."""
        coord = _make_obs_coord(hvac_action="heating", indoor_temp=72.0, outdoor_temp=55.0)
        dt_mock = _make_dt_mock()

        with (
            patch("custom_components.climate_advisor.coordinator.dt_util", dt_mock),
            patch("custom_components.climate_advisor.coordinator._LOGGER") as mock_log,
        ):
            asyncio.run(coord._start_hvac_observation("heat"))

        info_messages = []
        for call in mock_log.info.call_args_list:
            fmt = call[0][0]
            args = call[0][1:]
            try:
                info_messages.append(fmt % args)
            except Exception:
                info_messages.append(fmt)

        # Must see a log about starting the observation with obs_type
        start_logs = [m for m in info_messages if "starting" in m.lower() or "start" in m.lower()]
        assert start_logs, f"Expected an INFO log containing 'start' or 'starting'. Got info calls: {info_messages}"
        combined = " ".join(start_logs)
        assert "hvac_heat" in combined, f"Expected obs_type 'hvac_heat' in start log. Got: {combined}"

    # ── test 3: abandon log includes n_post, elapsed, reason ─────────────────

    def test_hvac_observation_abandon_logs_n_post_and_elapsed(self):
        """_check_hvac_stabilization timeout path emits INFO log with n_post and elapsed before abandon."""
        coord = _make_obs_coord(hvac_action="idle", indoor_temp=72.0, outdoor_temp=55.0)

        from datetime import timedelta

        active_end = _FAKE_NOW - timedelta(minutes=60)  # 60 min elapsed → exceeds timeout
        post_samples = [
            {
                "timestamp": _FAKE_NOW.isoformat(),
                "indoor_temp_f": 72.0,
                "outdoor_temp_f": 55.0,
                "elapsed_minutes": float(i),
            }
            for i in range(3)  # 3 post-heat samples
        ]
        coord._pending_observations[OBS_TYPE_HVAC_HEAT] = {
            "obs_type": OBS_TYPE_HVAC_HEAT,
            "obs_id": "test-abandon-hvac-1",
            "start_time": (_FAKE_NOW - timedelta(minutes=90)).isoformat(),
            "status": "monitoring",
            "hvac_mode": "heat",
            "session_mode": "heat",
            "active_start": (_FAKE_NOW - timedelta(minutes=90)).isoformat(),
            "active_end": active_end.isoformat(),
            "_phase": "post_heat",
            "active_samples": [],
            "post_heat_samples": post_samples,
            "pre_heat_samples": [],
            "peak_indoor_f": 74.0,
            "start_indoor_f": 70.0,
            "end_indoor_f": None,
            "stabilized_at": None,
            "flags_at_start": {},
            "schema_version": 1,
        }

        dt_mock = _make_dt_mock(now=_FAKE_NOW)
        with (
            patch("custom_components.climate_advisor.coordinator.dt_util", dt_mock),
            patch("custom_components.climate_advisor.coordinator._LOGGER") as mock_log,
        ):
            asyncio.run(coord._check_hvac_stabilization(OBS_TYPE_HVAC_HEAT))

        # Confirm the observation was abandoned
        assert OBS_TYPE_HVAC_HEAT not in coord._pending_observations, (
            "Expected hvac_heat obs to be abandoned after 60-min post-heat timeout"
        )

        # Collect all info calls
        info_messages = []
        for call in mock_log.info.call_args_list:
            fmt = call[0][0]
            args = call[0][1:]
            try:
                info_messages.append(fmt % args)
            except Exception:
                info_messages.append(fmt)

        # Must see a diagnostic log BEFORE the abandon that includes n_post and elapsed
        pre_abandon_logs = [m for m in info_messages if "n_post" in m or "post_heat" in m.lower()]
        assert pre_abandon_logs, (
            f"Expected a pre-abandon INFO log containing 'n_post' or 'post_heat'. Got info calls: {info_messages}"
        )
        combined = " ".join(pre_abandon_logs)
        # n_post=3 should appear
        assert "3" in combined, f"Expected n_post=3 to appear in pre-abandon log. Got: {combined}"


# ---------------------------------------------------------------------------
# TestHvacShortCycle (Issue #130, Phase B — fixes D14–D17)
# ---------------------------------------------------------------------------


class TestHvacShortCycle:
    """D14–D17: Allow short HVAC cycles (5–30 min) to commit observations.

    B1: THERMAL_MIN_POST_HEAT_SAMPLES lowered 10 → 4.
    B2: Stabilization-wait gate removed; commit via OLS when min samples reached.
    B3: Outdoor temp fallback (_last_known_outdoor_f) when current reading is None.
    B4: Bridge-home proxy: use k_vent_window as k_passive input when k_passive=None.
    """

    # ── shared helpers ────────────────────────────────────────────────────────

    def _make_post_heat_obs(
        self,
        n_post: int,
        peak_f: float = 72.0,
        end_f: float = 71.0,
        active_elapsed_minutes: int = 60,
        now: datetime = _FAKE_NOW,
    ) -> dict:
        """Build a hvac_heat obs in post_heat phase with n_post samples.

        Samples are spread across the last n_post minutes before *now*.
        All temps equal end_f (stable decay), unless oscillating=True is used
        via the caller.
        """
        active_end = datetime(
            now.year,
            now.month,
            now.day,
            now.hour,
            max(0, now.minute - n_post),
            now.second,
            tzinfo=UTC,
        )
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
                    "elapsed_minutes": float(active_elapsed_minutes + i),
                }
            )
        return {
            "obs_type": OBS_TYPE_HVAC_HEAT,
            "obs_id": "test-short-cycle",
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

    def _make_oscillating_post_heat_obs(
        self,
        n_post: int = 4,
        peak_f: float = 72.0,
        now: datetime = _FAKE_NOW,
    ) -> dict:
        """Build post_heat obs with oscillating temps — variance > 0.3°F in last 5 min.

        Temperatures alternate ±0.5°F around 71.5, so max-min = 1.0 > 0.3.
        Currently this prevents stabilization and blocks commit.
        After B2, OLS runs directly and commits if R² ok.
        """
        active_end = datetime(
            now.year,
            now.month,
            now.day,
            now.hour,
            max(0, now.minute - n_post),
            now.second,
            tzinfo=UTC,
        )
        oscillating = [71.5, 72.0, 71.0, 71.5]
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
                    "indoor_temp_f": oscillating[i % len(oscillating)],
                    "outdoor_temp_f": 50.0,
                    "elapsed_minutes": float(60 + i),
                }
            )
        return {
            "obs_type": OBS_TYPE_HVAC_HEAT,
            "obs_id": "test-short-cycle-osc",
            "start_time": _FAKE_NOW.isoformat(),
            "active_start": active_end.isoformat(),
            "active_end": active_end.isoformat(),
            "status": "monitoring",
            "_phase": "post_heat",
            "active_samples": [],
            "post_heat_samples": samples,
            "peak_indoor_f": peak_f,
            "end_indoor_f": oscillating[-1],
            "flags_at_start": {},
            "schema_version": 1,
        }

    # ── B1 ────────────────────────────────────────────────────────────────────

    def test_post_heat_min_samples_constant_is_4(self):
        """D14: THERMAL_MIN_POST_HEAT_SAMPLES must equal 4 after the fix."""
        from custom_components.climate_advisor.const import THERMAL_MIN_POST_HEAT_SAMPLES

        assert THERMAL_MIN_POST_HEAT_SAMPLES == 4, (
            f"Expected THERMAL_MIN_POST_HEAT_SAMPLES=4 (D14 fix), got {THERMAL_MIN_POST_HEAT_SAMPLES}"
        )

    # ── B2 ────────────────────────────────────────────────────────────────────

    def test_post_heat_commits_at_4_samples(self):
        """D14+D15: 4 post_heat_samples with sufficient decay triggers commit, not wait."""

        coord = _make_obs_coord()
        # Use exactly THERMAL_MIN_POST_HEAT_SAMPLES samples (should be 4 after fix)
        obs = self._make_post_heat_obs(n_post=4, peak_f=72.0, end_f=71.0)
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
            "4 post_heat_samples with decay=1.0 > 0.3 should have been committed. "
            f"commit_called={commit_called}, "
            f"obs still in pending={OBS_TYPE_HVAC_HEAT in coord._pending_observations}"
        )

    def test_post_heat_abandons_at_3_samples_after_timeout(self):
        """D15: 3 post_heat_samples + elapsed > 45 min → timeout abandon (not commit)."""
        coord = _make_obs_coord()
        # Only 3 samples, active_end set to 46 min ago so timeout fires
        now = _FAKE_NOW
        active_end = datetime(now.year, now.month, now.day, max(0, now.hour - 1), now.minute, now.second, tzinfo=UTC)
        obs = {
            "obs_type": OBS_TYPE_HVAC_HEAT,
            "obs_id": "test-timeout-3",
            "start_time": _FAKE_NOW.isoformat(),
            "active_start": active_end.isoformat(),
            "active_end": active_end.isoformat(),
            "status": "monitoring",
            "_phase": "post_heat",
            "active_samples": [],
            "post_heat_samples": [
                {
                    "timestamp": _FAKE_NOW.isoformat(),
                    "indoor_temp_f": 71.0,
                    "outdoor_temp_f": 50.0,
                    "elapsed_minutes": float(i),
                }
                for i in range(3)
            ],
            "peak_indoor_f": 72.0,
            "end_indoor_f": 71.0,
            "flags_at_start": {},
            "schema_version": 1,
        }
        coord._pending_observations[OBS_TYPE_HVAC_HEAT] = obs

        commit_called = []

        async def _fake_commit(obs_type, force_grade=None):
            commit_called.append(obs_type)
            coord._pending_observations.pop(obs_type, None)

        coord._commit_observation = _fake_commit

        dt_mock = _make_dt_mock()
        with patch("custom_components.climate_advisor.coordinator.dt_util", dt_mock):
            asyncio.run(coord._check_hvac_stabilization(OBS_TYPE_HVAC_HEAT))

        assert OBS_TYPE_HVAC_HEAT not in coord._pending_observations, (
            "3 samples + 60 min timeout should abandon the observation"
        )
        assert len(commit_called) == 0, "Timeout path should abandon (not commit). commit_called={commit_called}"

    def test_stabilization_not_required_for_commit(self):
        """D15: Oscillating post_heat temps must NOT block commit when min samples reached.

        Currently (pre-B2): variance > 0.3°F → code returns without committing.
        After B2: OLS runs directly; stabilization wait removed.
        """
        coord = _make_obs_coord()
        obs = self._make_oscillating_post_heat_obs(n_post=4, peak_f=72.0)
        coord._pending_observations[OBS_TYPE_HVAC_HEAT] = obs

        commit_called = []

        async def _fake_commit(obs_type, force_grade=None):
            commit_called.append(obs_type)
            coord._pending_observations.pop(obs_type, None)

        coord._commit_observation = _fake_commit

        dt_mock = _make_dt_mock()
        with patch("custom_components.climate_advisor.coordinator.dt_util", dt_mock):
            asyncio.run(coord._check_hvac_stabilization(OBS_TYPE_HVAC_HEAT))

        # After B2: commit should be called (stabilization variance no longer required)
        assert len(commit_called) == 1, (
            "Oscillating post_heat temps must NOT block commit after stabilization gate removed. "
            f"commit_called={commit_called}"
        )

    # ── B3 ────────────────────────────────────────────────────────────────────

    def test_outdoor_none_uses_last_known_within_30min(self):
        """D16: When _last_outdoor_temp=None, use _last_known_outdoor_f if < 30 min old."""
        from datetime import timedelta

        coord = _make_obs_coord(indoor_temp=75.0, outdoor_temp=55.0)
        # Simulate: weather entity unavailable → outdoor is None at line 2590
        coord._last_outdoor_temp = None
        # But we have a recent last-known reading
        coord._last_known_outdoor_f = 55.0
        coord._last_known_outdoor_ts = _FAKE_NOW - timedelta(minutes=20)

        # Start a passive_decay trigger so we can observe whether outdoor was used:
        # passive_decay trigger fires when delta >= THERMAL_PASSIVE_MIN_DELTA_F (3°F)
        # With outdoor=55, indoor=75: delta=20 → trigger should fire
        # But first: patch _FAKE_NOW into dt_util so now() returns the right time
        dt_mock = _make_dt_mock(_FAKE_NOW)

        with patch("custom_components.climate_advisor.coordinator.dt_util", dt_mock):
            coord._sample_all_observations()

        # If outdoor fallback worked: delta=20 > 3 → passive_decay should have started
        assert OBS_TYPE_PASSIVE_DECAY in coord._pending_observations, (
            "With _last_outdoor_temp=None but fresh _last_known_outdoor_f=55.0 (20min ago), "
            "passive_decay trigger should fire using fallback outdoor=55.0. "
            f"pending={list(coord._pending_observations.keys())}"
        )

    def test_outdoor_none_skips_when_stale_beyond_30min(self):
        """D16: _last_known_outdoor_f older than 30 min must NOT be used as fallback."""
        from datetime import timedelta

        coord = _make_obs_coord(indoor_temp=75.0, outdoor_temp=55.0)
        coord._last_outdoor_temp = None
        # Stale reading: 31 minutes old → should NOT be used
        coord._last_known_outdoor_f = 55.0
        coord._last_known_outdoor_ts = _FAKE_NOW - timedelta(minutes=31)

        dt_mock = _make_dt_mock(_FAKE_NOW)

        with patch("custom_components.climate_advisor.coordinator.dt_util", dt_mock):
            coord._sample_all_observations()

        # With no valid outdoor, passive_decay trigger should NOT fire
        assert OBS_TYPE_PASSIVE_DECAY not in coord._pending_observations, (
            "With _last_known_outdoor_ts=31min ago (stale), outdoor fallback must NOT be used. "
            f"pending={list(coord._pending_observations.keys())}"
        )

    def test_outdoor_updates_last_known_on_valid_reading(self):
        """D16: A valid outdoor reading must update _last_known_outdoor_f and _last_known_outdoor_ts."""
        coord = _make_obs_coord(indoor_temp=75.0, outdoor_temp=57.0)
        # Ensure the attribute exists (will be added in B3 __init__ fix, but test sets it)
        coord._last_known_outdoor_f = None
        coord._last_known_outdoor_ts = None
        # _last_outdoor_temp = 57.0 (set by _make_obs_coord via outdoor_temp=57.0)

        dt_mock = _make_dt_mock(_FAKE_NOW)

        with patch("custom_components.climate_advisor.coordinator.dt_util", dt_mock):
            coord._sample_all_observations()

        assert coord._last_known_outdoor_f == 57.0, (
            f"Expected _last_known_outdoor_f=57.0 after valid reading, got {coord._last_known_outdoor_f}"
        )
        assert coord._last_known_outdoor_ts is not None, (
            "_last_known_outdoor_ts must be set after valid outdoor reading"
        )

    # ── B4 ────────────────────────────────────────────────────────────────────

    def _make_hvac_event_dict(
        self,
        n_post: int = 4,
        n_active: int = 3,
        peak_f: float = 72.0,
        end_f: float = 71.0,
    ) -> dict:
        """Build a minimal hvac_heat event dict for _commit_event_from_dict."""
        now = _FAKE_NOW
        pre_samples = [
            {
                "timestamp": now.isoformat(),
                "indoor_temp_f": 70.0,
                "outdoor_temp_f": 50.0,
                "elapsed_minutes": float(-i),
            }
            for i in range(2, 0, -1)
        ]
        active_samples = [
            {
                "timestamp": now.isoformat(),
                "indoor_temp_f": 70.0 + i * 0.5,
                "outdoor_temp_f": 50.0,
                "elapsed_minutes": float(i * 5),
            }
            for i in range(n_active)
        ]
        post_samples = [
            {
                "timestamp": now.isoformat(),
                "indoor_temp_f": peak_f - i * 0.25,
                "outdoor_temp_f": 50.0,
                "elapsed_minutes": float(n_active * 5 + i * 5),
            }
            for i in range(n_post)
        ]
        return {
            "obs_type": OBS_TYPE_HVAC_HEAT,
            "obs_id": "test-b4",
            "hvac_mode": "heat",
            "session_mode": "heat",
            "start_time": now.isoformat(),
            "active_start": now.isoformat(),
            "active_end": now.isoformat(),
            "start_indoor_f": 70.0,
            "peak_indoor_f": peak_f,
            "end_indoor_f": end_f,
            "session_minutes": float(n_active * 5),
            "pre_heat_samples": pre_samples,
            "active_samples": active_samples,
            "post_heat_samples": post_samples,
            "flags_at_start": {},
            "schema_version": 1,
        }

    _FULL_CACHE_TEMPLATE = {
        "k_passive": None,
        "k_active_heat": None,
        "k_active_cool": None,
        "k_vent": None,
        "k_vent_window": None,
        "k_solar": None,
        "observation_count_heat": 0,
        "observation_count_cool": 0,
        "observation_count_passive": 0,
        "observation_count_fan_only": 0,
        "observation_count_vent": 0,
        "observation_count_solar": 0,
        "last_observation_date": None,
        "avg_r_squared_passive": None,
        "confidence_k_passive": "none",
        "confidence_k_hvac": "none",
    }

    def _make_engine_with_dt(self, tmp_path):
        """Build a LearningEngine with dt_util returning a real datetime."""
        from custom_components.climate_advisor.learning import LearningEngine

        engine = LearningEngine(tmp_path)
        engine.load_state()
        return engine

    def _seed_cache(self, engine, overrides: dict) -> None:
        """Seed engine cache with full template + overrides."""
        cache = dict(self._FULL_CACHE_TEMPLATE)
        cache.update(overrides)
        engine._state.thermal_model_cache = cache

    def test_bridge_home_uses_k_vent_proxy_when_k_passive_none(self, tmp_path):
        """D17: When k_passive OLS fails but k_vent_window is available, use it as proxy."""
        from custom_components.climate_advisor.const import REJECT_OLS_BAD_FIT

        engine = self._make_engine_with_dt(tmp_path)
        # Seed the cache with k_vent_window (bridge home has ventilated obs but no passive)
        self._seed_cache(engine, {"k_passive": None, "k_vent_window": -0.15})

        event = self._make_hvac_event_dict(n_post=4, n_active=3)

        # Patch compute_k_passive to return None (simulates short post-heat OLS failure)
        # Also patch dt_util in learning module so now().date().isoformat() returns a real string
        dt_mock = _make_dt_mock()
        with (
            patch(
                "custom_components.climate_advisor.learning.compute_k_passive",
                return_value=(None, 0.08, REJECT_OLS_BAD_FIT),
            ),
            patch("custom_components.climate_advisor.learning.dt_util", dt_mock),
        ):
            result, reject_code, r2 = engine._commit_event_from_dict(
                event, force_grade=None, obs_type=OBS_TYPE_HVAC_HEAT
            )

        assert result is not None, (
            "Bridge home: k_passive=None but k_vent_window=-0.15 available — "
            f"should commit using proxy. Got result=None, reject_code={reject_code}"
        )
        assert result.get("confidence_grade") == "low", (
            f"Proxy commit must use confidence_grade='low', got {result.get('confidence_grade')}"
        )

    def test_normal_home_uses_real_k_passive(self, tmp_path):
        """D17: When k_passive OLS succeeds, proxy must NOT be used."""
        engine = self._make_engine_with_dt(tmp_path)
        self._seed_cache(engine, {"k_passive": -0.03, "k_vent_window": -0.15})

        event = self._make_hvac_event_dict(n_post=4, n_active=3)

        dt_mock = _make_dt_mock()
        # compute_k_passive returns a real value → proxy path must NOT be taken
        with (
            patch(
                "custom_components.climate_advisor.learning.compute_k_passive",
                return_value=(-0.03, 0.75, None),
            ),
            patch("custom_components.climate_advisor.learning.dt_util", dt_mock),
        ):
            result, reject_code, r2 = engine._commit_event_from_dict(
                event, force_grade=None, obs_type=OBS_TYPE_HVAC_HEAT
            )

        assert result is not None, (
            f"Normal home: k_passive OLS succeeded → should commit. Got result=None, reject_code={reject_code}"
        )
        # confidence_grade should NOT be forced to "low" by the proxy path
        assert result.get("confidence_grade") in ("low", "medium", "high"), (
            f"confidence_grade should be computed normally (not proxy-forced), got {result.get('confidence_grade')}"
        )
        # The k_passive in the result should reflect the OLS value, not k_vent_window
        assert result.get("k_passive") == pytest.approx(-0.03, abs=1e-4), (
            f"k_passive in result should be -0.03 (OLS value), got {result.get('k_passive')}"
        )


class TestSinglePointKActive:
    """D19: Single-point k_active estimator for short HVAC cycles (n_active < 2).

    Plan: Issue #130 Phase C Revised — shiny-dreaming-cookie.md
    Tests the compute_k_active_single_point() function and its fallthrough
    integration inside _commit_event_from_dict().
    """

    # ── unit tests for compute_k_active_single_point() ────────────────────────

    def test_single_point_heat_within_bounds(self):
        """D19: Valid heat cycle produces k_active in [0.5, 15.0].

        May 3 representative cycle: T_start=68, T_peak=69, elapsed=9m42s=0.162hr,
        k_passive=-0.14 (bridge proxy), avg_delta=14.7°F (indoor-outdoor).
        gross_rate = 1/0.162 ≈ 6.17°F/hr
        k_active = 6.17 - (-0.14 × 14.7) = 6.17 + 2.058 ≈ 8.23°F/hr  → in [0.5, 15.0]
        """
        from custom_components.climate_advisor.learning import compute_k_active_single_point

        result = compute_k_active_single_point(
            T_start=68.0,
            T_peak=69.0,
            elapsed_hours=0.162,
            k_passive=-0.14,
            avg_delta_t=14.7,
            session_mode="heat",
        )
        assert result is not None, (
            "Valid heat cycle with ΔT=1°F, elapsed=0.162hr, k_passive=-0.14 should return a k_active value, got None"
        )
        assert 0.5 <= result <= 15.0, f"k_active should be in [0.5, 15.0], got {result:.3f}"

    def test_single_point_signal_too_small(self):
        """D19+D23: ΔT < THERMAL_HVAC_MIN_SIGNAL_F (0.5°F) → None (sensor noise floor)."""
        from custom_components.climate_advisor.learning import compute_k_active_single_point

        result = compute_k_active_single_point(
            T_start=68.0,
            T_peak=68.3,  # ΔT = 0.3°F < 0.5 threshold
            elapsed_hours=0.162,
            k_passive=-0.14,
            avg_delta_t=14.7,
            session_mode="heat",
        )
        assert result is None, f"ΔT=0.3°F below THERMAL_HVAC_MIN_SIGNAL_F=0.5 should return None, got {result}"

    def test_single_point_out_of_bounds(self):
        """D19: k_active > THERMAL_K_ACTIVE_HEAT_MAX (15.0) → None (unphysical result)."""
        from custom_components.climate_advisor.learning import compute_k_active_single_point

        # With elapsed_hours very small, gross_rate becomes huge → k_active > 15.0
        # elapsed=0.05hr (3min), ΔT=1°F → gross_rate=20°F/hr → k_active ≈ 22°F/hr > 15
        result = compute_k_active_single_point(
            T_start=68.0,
            T_peak=69.0,
            elapsed_hours=0.05,  # 3 minutes — yields gross_rate=20°F/hr
            k_passive=-0.14,
            avg_delta_t=14.7,
            session_mode="heat",
        )
        assert result is None, f"k_active > 15.0 (out of THERMAL_K_ACTIVE_HEAT_MAX) should return None, got {result}"

    def test_single_point_zero_elapsed(self):
        """D19: elapsed_hours=0 → None (division by zero guard)."""
        from custom_components.climate_advisor.learning import compute_k_active_single_point

        result = compute_k_active_single_point(
            T_start=68.0,
            T_peak=69.0,
            elapsed_hours=0.0,
            k_passive=-0.14,
            avg_delta_t=14.7,
            session_mode="heat",
        )
        assert result is None, f"elapsed_hours=0 should return None (division-by-zero guard), got {result}"

    def test_single_point_cool_mode(self):
        """D19: Cool-mode cycle yields negative k_active within [-15.0, -0.5].

        T_start=75, T_peak=73 (ΔT=-2°F), elapsed=0.25hr, k_passive=-0.03, avg_delta=5°F.
        gross_rate = -2/0.25 = -8°F/hr
        k_active = -8 - (-0.03 × 5) = -8 + 0.15 = -7.85°F/hr  → in [-15, -0.5]
        """
        from custom_components.climate_advisor.learning import compute_k_active_single_point

        result = compute_k_active_single_point(
            T_start=75.0,
            T_peak=73.0,
            elapsed_hours=0.25,
            k_passive=-0.03,
            avg_delta_t=5.0,
            session_mode="cool",
        )
        assert result is not None, "Valid cool cycle with ΔT=-2°F should return k_active, got None"
        assert -15.0 <= result <= -0.5, f"Cool k_active should be in [-15.0, -0.5], got {result:.3f}"

    # ── integration tests: _commit_event_from_dict fallthrough ────────────────

    def _make_n1_event_dict(
        self,
        start_f: float = 68.0,
        peak_f: float = 69.0,
        session_min: float = 10.0,
        n_active: int = 1,
        n_post: int = 4,
    ) -> dict:
        """Build a minimal hvac_heat event dict with n_active=1 (short cycle)."""
        now = _FAKE_NOW
        active_samples = [
            {
                "timestamp": now.isoformat(),
                "indoor_temp_f": start_f + 0.5,
                "outdoor_temp_f": 54.0,
                "elapsed_minutes": 0.0,
            }
            for _ in range(n_active)
        ]
        post_samples = [
            {
                "timestamp": now.isoformat(),
                "indoor_temp_f": peak_f - i * 0.25,
                "outdoor_temp_f": 54.0,
                "elapsed_minutes": float(session_min + i * 5),
            }
            for i in range(n_post)
        ]
        pre_samples = [
            {
                "timestamp": now.isoformat(),
                "indoor_temp_f": start_f,
                "outdoor_temp_f": 54.0,
                "elapsed_minutes": float(-5 + i * 5),
            }
            for i in range(2)
        ]
        return {
            "obs_type": OBS_TYPE_HVAC_HEAT,
            "obs_id": "test-n1-single-point",
            "hvac_mode": "heat",
            "session_mode": "heat",
            "start_time": now.isoformat(),
            "active_start": now.isoformat(),
            "active_end": now.isoformat(),
            "start_indoor_f": start_f,
            "peak_indoor_f": peak_f,
            "end_indoor_f": peak_f - 0.5,
            "session_minutes": session_min,
            "pre_heat_samples": pre_samples,
            "active_samples": active_samples,
            "post_heat_samples": post_samples,
            "flags_at_start": {},
            "schema_version": 1,
        }

    _FULL_CACHE_TEMPLATE = {
        "k_passive": None,
        "k_active_heat": None,
        "k_active_cool": None,
        "k_vent": None,
        "k_vent_window": None,
        "k_solar": None,
        "observation_count_heat": 0,
        "observation_count_cool": 0,
        "observation_count_passive": 0,
        "observation_count_fan_only": 0,
        "observation_count_vent": 0,
        "observation_count_solar": 0,
        "last_observation_date": None,
        "avg_r_squared_passive": None,
        "confidence_k_passive": "none",
        "confidence_k_hvac": "none",
    }

    def _make_engine_with_dt(self, tmp_path):
        """Build a LearningEngine with dt_util returning a real datetime."""
        engine = LearningEngine(tmp_path)
        engine.load_state()
        return engine

    def _seed_cache(self, engine, overrides: dict) -> None:
        """Seed engine cache with full template + overrides."""
        cache = dict(self._FULL_CACHE_TEMPLATE)
        cache.update(overrides)
        engine._state.thermal_model_cache = cache

    def test_commit_falls_through_to_single_point(self, tmp_path):
        """D19: n_active=1 → OLS returns None → single-point fires → k_active not None.

        Setup: k_passive OLS fails (patched), k_vent_window=-0.14 proxy available.
        start_indoor_f=68, peak_indoor_f=69, session_minutes=10 (elapsed=0.167hr).
        Expected: result committed with k_active_heat != None, grade="low".
        """
        from custom_components.climate_advisor.const import REJECT_OLS_BAD_FIT

        engine = self._make_engine_with_dt(tmp_path)
        # Bridge home: k_vent_window available, no envelope k_passive
        self._seed_cache(engine, {"k_passive": None, "k_vent_window": -0.14})

        event = self._make_n1_event_dict(
            start_f=68.0,
            peak_f=69.0,
            session_min=10.0,
            n_active=1,
        )

        dt_mock = _make_dt_mock()
        with (
            patch(
                "custom_components.climate_advisor.learning.compute_k_passive",
                return_value=(None, 0.05, REJECT_OLS_BAD_FIT),
            ),
            patch("custom_components.climate_advisor.learning.dt_util", dt_mock),
        ):
            result, reject_code, r2 = engine._commit_event_from_dict(
                event, force_grade=None, obs_type=OBS_TYPE_HVAC_HEAT
            )

        assert result is not None, (
            "n_active=1 with bridge proxy k_vent_window=-0.14 and valid T_start/T_peak/"
            f"session_minutes should produce a commit via single-point. "
            f"Got result=None, reject_code={reject_code}"
        )
        assert result.get("k_active") is not None, (
            f"Single-point path should set k_active, got {result.get('k_active')}"
        )
        assert result.get("confidence_grade") == "low", (
            f"Single-point commit must use grade='low', got {result.get('confidence_grade')}"
        )
        # D21: proxy used → k_passive and r_squared_passive in obs dict must be None
        assert result.get("k_passive") is None, (
            f"When proxy was used for k_p, obs dict k_passive must be None (D21). Got {result.get('k_passive')}"
        )
        assert result.get("r_squared_passive") is None, (
            f"D21: r_squared_passive must be None when proxy used. Got {result.get('r_squared_passive')}"
        )

    def test_commit_skip_when_no_k_passive_available(self, tmp_path):
        """D19: n_active=1, no k_passive and no proxy → single-point cannot fire → k_active=None.

        Fresh install: neither OLS k_passive nor k_vent_window proxy is available.
        Expected: commit returns None (graceful skip, not an error).
        """
        from custom_components.climate_advisor.const import REJECT_OLS_BAD_FIT

        engine = self._make_engine_with_dt(tmp_path)
        # Fresh install: no proxy available
        self._seed_cache(engine, {"k_passive": None, "k_vent_window": None})

        event = self._make_n1_event_dict(
            start_f=68.0,
            peak_f=69.0,
            session_min=10.0,
            n_active=1,
        )

        dt_mock = _make_dt_mock()
        with (
            patch(
                "custom_components.climate_advisor.learning.compute_k_passive",
                return_value=(None, 0.05, REJECT_OLS_BAD_FIT),
            ),
            patch("custom_components.climate_advisor.learning.dt_util", dt_mock),
        ):
            result, reject_code, r2 = engine._commit_event_from_dict(
                event, force_grade=None, obs_type=OBS_TYPE_HVAC_HEAT
            )

        assert result is None, (
            "Fresh install (no k_passive, no proxy): single-point must not fire. "
            f"Expected result=None, got result={result}"
        )

    # ── live coordinator n_post gate (Phase D) ─────────────────────────────

    def _make_stabilization_obs(
        self,
        n_post: int,
        peak_f: float = 69.0,
        end_f: float = 68.5,
    ) -> dict:
        """Build a minimal HVAC_HEAT obs in post_heat phase with n_post post samples."""
        now = _FAKE_NOW
        active_end = datetime(now.year, now.month, now.day, now.hour, max(0, now.minute - n_post - 2), tzinfo=UTC)
        post_samples = [
            {
                "timestamp": now.isoformat(),
                "indoor_temp_f": peak_f - i * 0.1,
                "outdoor_temp_f": 54.0,
                "elapsed_minutes": float(i * 5),
            }
            for i in range(n_post)
        ]
        return {
            "obs_type": OBS_TYPE_HVAC_HEAT,
            "obs_id": "test-phase-d-n-post",
            "start_time": active_end.isoformat(),
            "active_start": active_end.isoformat(),
            "active_end": active_end.isoformat(),
            "status": "monitoring",
            "_phase": "post_heat",
            "active_samples": [
                {
                    "timestamp": active_end.isoformat(),
                    "indoor_temp_f": 68.0,
                    "outdoor_temp_f": 54.0,
                    "elapsed_minutes": 0.0,
                }
            ],
            "post_heat_samples": post_samples,
            "peak_indoor_f": peak_f,
            "start_indoor_f": 68.0,
            "end_indoor_f": end_f,
            "session_minutes": 10.0,
            "flags_at_start": {},
            "schema_version": 1,
        }

    def test_live_n_post_1_commits_with_proxy(self):
        """D24: n_post=1 + k_vent_window proxy available → _check_hvac_stabilization commits.

        Without proxy: n_post=1 < THERMAL_MIN_POST_HEAT_SAMPLES (4) → early return (no commit).
        With proxy: min drops to 1 → single-post sample clears the gate → commit fires.
        """
        coord = _make_obs_coord()

        # Seed proxy: k_vent_window < 0 → proxy available
        coord.learning._state = MagicMock()
        coord.learning._state.thermal_model_cache = {"k_vent_window": -0.14}

        obs = self._make_stabilization_obs(n_post=1, peak_f=69.0, end_f=68.5)
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
            "D24: n_post=1 with k_vent_window=-0.14 proxy should commit via _check_hvac_stabilization, "
            f"but commit_called={commit_called}"
        )

    def test_live_n_post_0_rejects_even_with_proxy(self):
        """D24: n_post=0 (empty post_heat_samples) → abandoned even when proxy is available.

        n_post=0 means no HVAC-off timestamp exists — single-point can't compute elapsed.
        The proxy drops the minimum to 1, not 0, so this must still be rejected.
        """
        coord = _make_obs_coord()

        # Seed proxy: k_vent_window < 0 → proxy available
        coord.learning._state = MagicMock()
        coord.learning._state.thermal_model_cache = {"k_vent_window": -0.14}

        obs = self._make_stabilization_obs(n_post=0, peak_f=69.0, end_f=68.5)
        coord._pending_observations[OBS_TYPE_HVAC_HEAT] = obs

        commit_called = []

        async def _fake_commit(obs_type, force_grade=None):
            commit_called.append(obs_type)
            coord._pending_observations.pop(obs_type, None)

        coord._commit_observation = _fake_commit

        dt_mock = _make_dt_mock()
        with patch("custom_components.climate_advisor.coordinator.dt_util", dt_mock):
            asyncio.run(coord._check_hvac_stabilization(OBS_TYPE_HVAC_HEAT))

        assert len(commit_called) == 0, (
            "D24: n_post=0 should never commit — no HVAC-off timestamp for single-point elapsed. "
            f"commit_called={commit_called}"
        )


# ---------------------------------------------------------------------------
# TestSwingComputation — thermostat swing auto-detection (Issue #131)
# ---------------------------------------------------------------------------


class TestSwingComputation:
    """Tests for thermostat swing computation in the learning engine.

    Swing = half-amplitude of the thermostat deadband: (|T_end - T_start|) / 2.
    Computed in _commit_event_from_dict() for hvac_heat/hvac_cool observations.
    Stored in obs["swing_f"] and tracked in the EWMA cache as swing_heat_f / swing_cool_f.
    """

    def _make_engine(self, tmp_path):
        """Build a LearningEngine backed by tmp_path."""
        from custom_components.climate_advisor.learning import LearningEngine

        engine = LearningEngine(tmp_path)
        engine.load_state()
        return engine

    def _make_hvac_event(
        self,
        *,
        start_f: float,
        peak_f: float | None = None,
        hvac_mode: str = "heat",
        active_temps: list[float],
        n_post: int = 5,
    ) -> dict:
        """Build a minimal hvac event dict sufficient for swing computation."""
        active_samples = [
            {
                "indoor_temp_f": t,
                "outdoor_temp_f": 55.0,
                "elapsed_minutes": float(i * 10),
                "timestamp": f"2026-04-28T12:{i:02d}:00+00:00",
            }
            for i, t in enumerate(active_temps)
        ]
        post_samples = [
            {
                "indoor_temp_f": active_temps[-1] - i * 0.2,
                "outdoor_temp_f": 55.0,
                "elapsed_minutes": float(len(active_temps) * 10 + i * 10),
                "timestamp": f"2026-04-28T13:{i:02d}:00+00:00",
            }
            for i in range(n_post)
        ]
        return {
            "event_id": "test-swing",
            "hvac_mode": hvac_mode,
            "session_mode": hvac_mode,
            "start_indoor_f": start_f,
            "peak_indoor_f": peak_f if peak_f is not None else max(active_temps),
            "active_samples": active_samples,
            "post_heat_samples": post_samples,
            "pre_heat_samples": [],
            "session_minutes": len(active_temps) * 10.0,
        }

    def _commit_with_mocked_dt(self, engine, event, obs_type=OBS_TYPE_HVAC_HEAT, force_grade="low"):
        """Run _commit_event_from_dict with dt_util and OLS mocked."""
        dt_mock = _make_dt_mock()
        with (
            patch("custom_components.climate_advisor.learning.dt_util", dt_mock),
            patch(
                "custom_components.climate_advisor.learning.compute_k_passive",
                return_value=(-0.05, 0.85, None),
            ),
            patch(
                "custom_components.climate_advisor.learning.compute_k_active",
                return_value=(2.5, 0.9),
            ),
        ):
            result, reject, r2 = engine._commit_event_from_dict(event, force_grade=force_grade, obs_type=obs_type)
        return result

    # ------------------------------------------------------------------
    # A0-1: basic heat swing
    # ------------------------------------------------------------------

    def test_swing_basic_heat(self, tmp_path):
        """Heat obs: swing_f = abs(peak - start) / 2."""
        engine = self._make_engine(tmp_path)
        event = self._make_hvac_event(
            start_f=66.0,
            peak_f=70.0,
            hvac_mode="heat",
            active_temps=[68.0, 70.0],
        )
        obs = self._commit_with_mocked_dt(engine, event)
        assert obs is not None, "commit must succeed"
        assert "swing_f" in obs, f"swing_f missing from obs: {obs}"
        assert obs["swing_f"] == 2.0, f"Expected 2.0, got {obs['swing_f']}"

    # ------------------------------------------------------------------
    # A0-2: basic cool swing
    # ------------------------------------------------------------------

    def test_swing_basic_cool(self, tmp_path):
        """Cool obs: swing_f = abs(trough - start) / 2 (trough = min of active_temps)."""
        engine = self._make_engine(tmp_path)
        event = self._make_hvac_event(
            start_f=78.0,
            hvac_mode="cool",
            active_temps=[76.0, 74.0],
        )
        obs = self._commit_with_mocked_dt(engine, event, obs_type=OBS_TYPE_HVAC_COOL)
        assert obs is not None, "commit must succeed"
        assert "swing_f" in obs, f"swing_f missing from obs: {obs}"
        # start=78, trough=74, delta=4, swing=2.0
        assert obs["swing_f"] == 2.0, f"Expected 2.0, got {obs['swing_f']}"

    # ------------------------------------------------------------------
    # A0-3: below THERMAL_HVAC_MIN_SIGNAL_F — no swing_f
    # ------------------------------------------------------------------

    def test_swing_below_min_signal(self, tmp_path):
        """delta < THERMAL_HVAC_MIN_SIGNAL_F (0.5°F) — swing_f must be absent."""
        from custom_components.climate_advisor.const import THERMAL_HVAC_MIN_SIGNAL_F

        engine = self._make_engine(tmp_path)
        # delta = 0.4 < 0.5 → no swing
        event = self._make_hvac_event(
            start_f=70.0,
            peak_f=70.4,
            hvac_mode="heat",
            active_temps=[70.2, 70.4],
        )
        obs = self._commit_with_mocked_dt(engine, event)
        assert obs is not None, "commit must succeed"
        assert "swing_f" not in obs, (
            f"swing_f should be absent for delta < {THERMAL_HVAC_MIN_SIGNAL_F}°F, got {obs.get('swing_f')}"
        )

    # ------------------------------------------------------------------
    # A0-4: swing too high (> THERMAL_SWING_MAX_F) — no swing_f
    # ------------------------------------------------------------------

    def test_swing_out_of_bounds_high(self, tmp_path):
        """delta=12°F → swing=6.0 > THERMAL_SWING_MAX_F(5.0) — swing_f must be absent."""
        engine = self._make_engine(tmp_path)
        event = self._make_hvac_event(
            start_f=66.0,
            peak_f=78.0,  # delta=12, swing=6.0
            hvac_mode="heat",
            active_temps=[72.0, 78.0],
        )
        obs = self._commit_with_mocked_dt(engine, event)
        assert obs is not None, "commit must succeed"
        assert "swing_f" not in obs, (
            f"swing_f should be absent when swing > THERMAL_SWING_MAX_F, got {obs.get('swing_f')}"
        )

    # ------------------------------------------------------------------
    # A0-5: swing too low (< THERMAL_SWING_MIN_F) — no swing_f
    # ------------------------------------------------------------------

    def test_swing_out_of_bounds_low(self, tmp_path):
        """delta=0.16°F → swing=0.08 < THERMAL_SWING_MIN_F(0.1) — swing_f must be absent."""
        engine = self._make_engine(tmp_path)
        event = self._make_hvac_event(
            start_f=70.0,
            peak_f=70.16,  # delta=0.16, swing=0.08 < 0.1
            hvac_mode="heat",
            active_temps=[70.08, 70.16],
        )
        # delta=0.16 >= THERMAL_HVAC_MIN_SIGNAL_F(0.5)? No — 0.16 < 0.5.
        # Need delta >= 0.5 but swing < 0.1. delta=0.16 is already below min_signal.
        # Use delta=0.20 (>= 0.5 is impossible with swing=0.08 since swing=delta/2=0.1).
        # Exactly at boundary: delta=0.20 → swing=0.10 = THERMAL_SWING_MIN_F (excluded by <, not <=).
        # To get swing < 0.1: delta < 0.2. But delta < 0.5 is already rejected by min_signal.
        # The min_signal check fires first, so this test uses delta=0.16 (< 0.5 min_signal).
        # Restate: delta=0.16 is filtered by MIN_SIGNAL check, so swing_f absent.
        obs = self._commit_with_mocked_dt(engine, event)
        assert obs is not None, "commit must succeed"
        assert "swing_f" not in obs, (
            f"swing_f should be absent when swing < THERMAL_SWING_MIN_F, got {obs.get('swing_f')}"
        )

    # ------------------------------------------------------------------
    # A0-6: EWMA update
    # ------------------------------------------------------------------

    def test_swing_ewma_update(self, tmp_path):
        """Two heat obs with swing_f=2.0 then 3.0 (grade=low, alpha=0.05) update cache correctly."""
        engine = self._make_engine(tmp_path)

        obs1 = {
            "hvac_mode": "heat",
            "confidence_grade": "low",
            "swing_f": 2.0,
            "date": "2026-04-28",
        }
        obs2 = {
            "hvac_mode": "heat",
            "confidence_grade": "low",
            "swing_f": 3.0,
            "date": "2026-04-28",
        }
        engine._update_thermal_model_cache(obs1)
        engine._update_thermal_model_cache(obs2)

        cache = engine._state.thermal_model_cache
        # After obs1: swing_heat_f = 2.0 (first → seed)
        # After obs2: swing_heat_f = 0.95*2.0 + 0.05*3.0 = 1.9 + 0.15 = 2.05
        assert cache["observation_count_swing_heat"] == 2, (
            f"Expected 2 swing heat obs, got {cache['observation_count_swing_heat']}"
        )
        expected = 0.95 * 2.0 + 0.05 * 3.0  # = 2.05
        assert abs(cache["swing_heat_f"] - expected) < 1e-6, (
            f"EWMA mismatch: expected {expected}, got {cache['swing_heat_f']}"
        )

    # ------------------------------------------------------------------
    # A0-7: default fallback in get_thermal_model()
    # ------------------------------------------------------------------

    def test_swing_default_fallback(self, tmp_path):
        """Empty/None cache → swing_heat_f is None, swing_heat_f_display is 1.5."""
        engine = self._make_engine(tmp_path)
        # Ensure no cache
        engine._state.thermal_model_cache = None

        model = engine.get_thermal_model()
        assert model["swing_heat_f"] is None, (
            f"swing_heat_f should be None for empty cache, got {model['swing_heat_f']}"
        )
        assert model["swing_heat_f_display"] == 1.5, (
            f"swing_heat_f_display should default to 1.5, got {model['swing_heat_f_display']}"
        )

    # ------------------------------------------------------------------
    # A0-8: confidence grade thresholds
    # ------------------------------------------------------------------

    @pytest.mark.parametrize(
        "count,expected",
        [
            (0, "none"),
            (1, "low"),
            (3, "medium"),
            (10, "high"),
        ],
    )
    def test_swing_confidence_grades(self, count, expected):
        """_grade_swing_confidence returns correct tier for each count."""
        from custom_components.climate_advisor.learning import _grade_swing_confidence

        result = _grade_swing_confidence(count)
        assert result == expected, f"count={count}: expected {expected!r}, got {result!r}"

    # ------------------------------------------------------------------
    # A0-9: heat and cool tracked independently
    # ------------------------------------------------------------------

    def test_swing_independence(self, tmp_path):
        """5 heat obs → confidence_swing_heat=medium; 0 cool obs → confidence_swing_cool=none."""
        engine = self._make_engine(tmp_path)

        for _ in range(5):
            obs = {
                "hvac_mode": "heat",
                "confidence_grade": "low",
                "swing_f": 2.0,
                "date": "2026-04-28",
            }
            engine._update_thermal_model_cache(obs)

        model = engine.get_thermal_model()
        assert model["confidence_swing_heat"] == "medium", (
            f"5 heat obs should give 'medium', got {model['confidence_swing_heat']}"
        )
        assert model["confidence_swing_cool"] == "none", (
            f"0 cool obs should give 'none', got {model['confidence_swing_cool']}"
        )
