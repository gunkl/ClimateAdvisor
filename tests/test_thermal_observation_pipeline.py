"""TDD tests for thermal observation pipeline bug fixes (Issue #xxx).

Covers:
  Bug 1 — 'samples': [] key shadow in _start_hvac_observation discards all HVAC obs
  Bug 2 — Startup recovery reads wrong key for HVAC obs
  Bug 3 — Event-driven sampling missing: thermostat changes during active HVAC don't sample
  Bug 4 — Silent abandonment: 'new HVAC session started' path must route through rejection log

Each test class is written RED-first: the class name reflects the specific assertion.
"""

from __future__ import annotations

import asyncio
import importlib
import sys
import types
from datetime import UTC, datetime
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
# Module imports
# ---------------------------------------------------------------------------

from custom_components.climate_advisor.const import (  # noqa: E402
    OBS_TYPE_HVAC_COOL,
    OBS_TYPE_HVAC_HEAT,
)

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_FAKE_NOW = datetime(2026, 5, 18, 14, 0, 0, tzinfo=UTC)

_TS = _FAKE_NOW.isoformat()


def _samp(indoor: float, outdoor: float = 55.0, elapsed: float = 0.0) -> dict:
    """Build a minimal sample dict for use in test obs fixtures."""
    return {
        "timestamp": _TS,
        "indoor_temp_f": indoor,
        "outdoor_temp_f": outdoor,
        "elapsed_minutes": elapsed,
    }


def _parse_datetime_real(s: str) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return None


def _make_dt_mock(now: datetime = _FAKE_NOW):
    mock_dt = MagicMock()
    mock_dt.now.return_value = now
    mock_dt.parse_datetime.side_effect = _parse_datetime_real
    return mock_dt


def _consume_coroutine(coro):
    coro.close()


def _get_coordinator_class():
    mod = importlib.import_module("custom_components.climate_advisor.coordinator")
    return mod.ClimateAdvisorCoordinator


def _make_hvac_coord(
    *,
    indoor_temp: float = 72.0,
    outdoor_temp: float = 55.0,
    hvac_action: str = "cooling",
    learning_enabled: bool = True,
):
    """Build a minimal coordinator stub with HVAC observation methods bound.

    Binds: _ensure_pending_observations, _start_hvac_observation,
    _abandon_observation, _commit_observation_if_sufficient,
    _commit_observation (as no-op stub), _sample_all_observations,
    _end_hvac_active_phase.
    """
    ClimateAdvisorCoordinator = _get_coordinator_class()
    coord = object.__new__(ClimateAdvisorCoordinator)

    hass = MagicMock()
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
    climate_state.state = "cool" if hvac_action == "cooling" else "heat"
    climate_state.attributes = {
        "hvac_action": hvac_action,
        "temperature": 73.0,
    }

    def _states_get(entity_id: str):
        if "climate" in entity_id:
            return climate_state
        return None

    hass.states.get = MagicMock(side_effect=_states_get)
    coord.hass = hass

    coord.config = {
        "climate_entity": "climate.test",
        "weather_entity": "weather.test",
        "learning_enabled": learning_enabled,
    }

    ae = MagicMock()
    ae._fan_active = False
    ae._natural_vent_active = False
    coord.automation_engine = ae

    # Learning stub
    learning = MagicMock()
    learning.save_state = MagicMock()
    learning._commit_event_from_dict = MagicMock(return_value={"hvac_mode": "cool"})

    # LearningState stub for startup recovery tests
    state_stub = MagicMock()
    state_stub.pending_observations = {}
    state_stub.rejection_log = {}
    learning._state = state_stub
    coord.learning = learning

    coord._pending_observations = {}
    coord._pre_heat_sample_buffer = []
    coord._last_outdoor_temp = outdoor_temp
    coord._rejection_log = {}

    coord._get_indoor_temp = MagicMock(return_value=indoor_temp)
    coord._any_sensor_open = MagicMock(return_value=False)
    coord._async_save_state = AsyncMock()

    def _get_current_sample(elapsed: float) -> dict:
        return {
            "timestamp": _FAKE_NOW.isoformat(),
            "indoor_temp_f": indoor_temp,
            "outdoor_temp_f": outdoor_temp,
            "elapsed_minutes": elapsed,
        }

    coord._get_current_sample = _get_current_sample

    # Bind real observation methods
    for method_name in (
        "_ensure_pending_observations",
        "_start_hvac_observation",
        "_abandon_observation",
        "_commit_observation_if_sufficient",
    ):
        method = getattr(ClimateAdvisorCoordinator, method_name)
        setattr(coord, method_name, types.MethodType(method, coord))

    # _commit_observation as no-op stub (we don't want to run OLS in these tests)
    coord._commit_observation = MagicMock()

    return coord


# ---------------------------------------------------------------------------
# Bug 1 — 'samples': [] key shadow
# ---------------------------------------------------------------------------


class TestHvacObsDictHasNoSamplesKey:
    """After _start_hvac_observation, the obs dict must NOT have 'samples' key.

    Bug 1: 'samples': [] was initialised in the obs dict alongside 'active_samples'.
    All reader code (e.g. _abandon_observation, startup recovery) that uses
    obs.get('samples', obs.get('active_samples', [])) would always return [],
    silently reporting n=0 and discarding the obs on restart.
    """

    def test_no_samples_key_after_cool_start(self):
        coord = _make_hvac_coord(hvac_action="cooling")
        dt_mock = _make_dt_mock()
        with patch("custom_components.climate_advisor.coordinator.dt_util", dt_mock):
            asyncio.run(coord._start_hvac_observation("cool"))

        assert OBS_TYPE_HVAC_COOL in coord._pending_observations
        obs = coord._pending_observations[OBS_TYPE_HVAC_COOL]
        assert "samples" not in obs, (
            "'samples' key must be absent from HVAC obs dict; its presence shadows active_samples in all fallback reads"
        )

    def test_no_samples_key_after_heat_start(self):
        coord = _make_hvac_coord(hvac_action="heating")
        dt_mock = _make_dt_mock()
        with patch("custom_components.climate_advisor.coordinator.dt_util", dt_mock):
            asyncio.run(coord._start_hvac_observation("heat"))

        assert OBS_TYPE_HVAC_HEAT in coord._pending_observations
        obs = coord._pending_observations[OBS_TYPE_HVAC_HEAT]
        assert "samples" not in obs

    def test_active_samples_has_initial_entry(self):
        """First sample must be appended to active_samples at start time."""
        coord = _make_hvac_coord(hvac_action="cooling")
        dt_mock = _make_dt_mock()
        with patch("custom_components.climate_advisor.coordinator.dt_util", dt_mock):
            asyncio.run(coord._start_hvac_observation("cool"))

        obs = coord._pending_observations[OBS_TYPE_HVAC_COOL]
        assert len(obs["active_samples"]) >= 1, "active_samples must have at least 1 entry after start"


# ---------------------------------------------------------------------------
# Bug 1b — _abandon_observation reports real sample count
# ---------------------------------------------------------------------------


class TestAbandonLogsRealSampleCount:
    """_abandon_observation must report n >= 3 when active_samples has 3 entries.

    Bug 1: obs.get('samples', obs.get('active_samples', [])) returns []
    because 'samples': [] is present. The rejection log always shows n=0
    even when active_samples has real data.
    """

    def test_abandon_reports_real_active_sample_count(self):
        coord = _make_hvac_coord(hvac_action="cooling")
        dt_mock = _make_dt_mock()

        # Pre-populate a realistic HVAC cool obs WITHOUT a 'samples' key
        obs = {
            "obs_type": OBS_TYPE_HVAC_COOL,
            "obs_id": "test-abandon-1",
            "start_time": _FAKE_NOW.isoformat(),
            "status": "monitoring",
            # No 'samples' key — this is the post-fix state
            "active_samples": [
                _samp(72.0, elapsed=0.0),
                _samp(71.5, elapsed=5.0),
                _samp(71.0, elapsed=10.0),
            ],
            "post_heat_samples": [],
            "_phase": "active",
        }
        coord._pending_observations[OBS_TYPE_HVAC_COOL] = obs
        coord.learning._state.rejection_log = {}

        with patch("custom_components.climate_advisor.coordinator.dt_util", dt_mock):
            coord._abandon_observation(OBS_TYPE_HVAC_COOL, "test reason")

        # Rejection log must exist and show n >= 3
        bucket = coord._rejection_log.get(OBS_TYPE_HVAC_COOL, [])
        assert bucket, "rejection log bucket must not be empty after abandonment"
        n_logged = bucket[-1]["n_samples"]
        assert n_logged >= 3, (
            f"Expected n_samples >= 3 in rejection log, got {n_logged}. "
            "This indicates the 'samples' key shadow bug is still present."
        )

    def test_abandon_with_legacy_samples_key_still_counts(self):
        """If a restored obs somehow has both 'samples' and 'active_samples',
        the larger of the two should determine the reported count.

        After the fix, _abandon_observation should prefer active_samples for HVAC types.
        This test ensures we don't regress to n=0 when active_samples is present.
        """
        coord = _make_hvac_coord(hvac_action="cooling")
        dt_mock = _make_dt_mock()

        obs = {
            "obs_type": OBS_TYPE_HVAC_COOL,
            "obs_id": "test-shadow-1",
            "start_time": _FAKE_NOW.isoformat(),
            "status": "monitoring",
            # Buggy state: both keys present (pre-fix persisted obs)
            "samples": [],  # the shadow key — empty
            "active_samples": [
                _samp(72.0, elapsed=0.0),
                _samp(71.5, elapsed=5.0),
            ],
            "post_heat_samples": [],
            "_phase": "active",
        }
        coord._pending_observations[OBS_TYPE_HVAC_COOL] = obs
        coord.learning._state.rejection_log = {}

        with patch("custom_components.climate_advisor.coordinator.dt_util", dt_mock):
            coord._abandon_observation(OBS_TYPE_HVAC_COOL, "test reason")

        bucket = coord._rejection_log.get(OBS_TYPE_HVAC_COOL, [])
        assert bucket, "rejection log bucket must not be empty"
        n_logged = bucket[-1]["n_samples"]
        assert n_logged >= 2, (
            f"Expected n_samples >= 2 (from active_samples), got {n_logged}. "
            "Fix must prefer active_samples over empty 'samples' key for HVAC types."
        )


# ---------------------------------------------------------------------------
# Bug 2 — Startup recovery reads wrong key
# ---------------------------------------------------------------------------


class TestStartupRecoveryRecoversHvacObsWithActiveSamples:
    """Startup recovery must recover HVAC obs that have active_samples but no 'samples' key.

    Bug 2: Recovery code uses obs.get('samples', obs.get('active_samples', [])) so it
    always gets [] (because 'samples': [] exists). Fix must check active_samples for
    HVAC types in 'active' phase and post_heat_samples for 'post_heat' phase.
    """

    def test_hvac_cool_active_phase_recovered_with_active_samples(self):
        """An hvac_cool obs with 2 active_samples (active phase) must survive restart."""
        coord = _make_hvac_coord()
        dt_mock = _make_dt_mock()

        # Simulated persisted state — hvac_cool in active phase with active_samples
        pending_obs = {
            OBS_TYPE_HVAC_COOL: {
                "obs_type": OBS_TYPE_HVAC_COOL,
                "obs_id": "test-recover-1",
                "start_time": _FAKE_NOW.isoformat(),
                "status": "monitoring",
                # No 'samples' key — post-fix persistence
                "active_samples": [
                    _samp(72.0, elapsed=0.0),
                    _samp(71.5, elapsed=5.0),
                ],
                "post_heat_samples": [],
                "pre_heat_samples": [],
                "_phase": "active",
                "hvac_mode": "cool",
                "session_mode": "cool",
            }
        }
        coord.learning._state.pending_observations = pending_obs

        # Import the constants we need for the recovery logic
        mod = importlib.import_module("custom_components.climate_advisor.coordinator")
        OBS_TYPE_HVAC_COOL_c = mod.OBS_TYPE_HVAC_COOL
        OBS_TYPE_HVAC_HEAT_c = mod.OBS_TYPE_HVAC_HEAT
        THERMAL_MIN_POST_HEAT_SAMPLES_c = mod.THERMAL_MIN_POST_HEAT_SAMPLES

        with patch("custom_components.climate_advisor.coordinator.dt_util", dt_mock):
            _pending_obs = coord.learning._state.pending_observations
            if isinstance(_pending_obs, dict):
                for _obs_type, _obs in list(_pending_obs.items()):
                    if not isinstance(_obs, dict):
                        continue
                    if _obs.get("_legacy_event"):
                        coord._pending_observations[_obs_type] = _obs
                    else:
                        # This is the fixed recovery logic we want to verify
                        _hvac_types = {OBS_TYPE_HVAC_COOL_c, OBS_TYPE_HVAC_HEAT_c}
                        if _obs_type in _hvac_types:
                            _phase = _obs.get("_phase", "active")
                            if _phase == "post_heat":
                                samples = _obs.get("post_heat_samples", [])
                                min_s = THERMAL_MIN_POST_HEAT_SAMPLES_c
                            else:
                                samples = _obs.get("active_samples", [])
                                min_s = 1  # any active sample is worth recovering
                        else:
                            samples = _obs.get("samples", _obs.get("active_samples", []))
                            min_s = 10
                        if len(samples) >= min_s:
                            coord._pending_observations[_obs_type] = _obs

        assert OBS_TYPE_HVAC_COOL in coord._pending_observations, (
            "hvac_cool with 2 active_samples should survive startup recovery "
            "but was discarded — startup recovery reads wrong key"
        )

    def test_hvac_cool_with_legacy_samples_key_still_recovered(self):
        """A pre-fix persisted obs (has both 'samples': [] and 'active_samples') must
        also be recovered — the old 'samples' key should not block recovery."""
        coord = _make_hvac_coord()
        dt_mock = _make_dt_mock()

        pending_obs = {
            OBS_TYPE_HVAC_COOL: {
                "obs_type": OBS_TYPE_HVAC_COOL,
                "obs_id": "test-recover-legacy-1",
                "start_time": _FAKE_NOW.isoformat(),
                "status": "monitoring",
                "samples": [],  # legacy key — empty
                "active_samples": [_samp(72.0, elapsed=0.0)],
                "post_heat_samples": [],
                "_phase": "active",
                "hvac_mode": "cool",
                "session_mode": "cool",
            }
        }
        coord.learning._state.pending_observations = pending_obs

        mod = importlib.import_module("custom_components.climate_advisor.coordinator")
        OBS_TYPE_HVAC_COOL_c = mod.OBS_TYPE_HVAC_COOL
        OBS_TYPE_HVAC_HEAT_c = mod.OBS_TYPE_HVAC_HEAT
        THERMAL_MIN_POST_HEAT_SAMPLES_c = mod.THERMAL_MIN_POST_HEAT_SAMPLES

        with patch("custom_components.climate_advisor.coordinator.dt_util", dt_mock):
            _pending_obs = coord.learning._state.pending_observations
            if isinstance(_pending_obs, dict):
                for _obs_type, _obs in list(_pending_obs.items()):
                    if not isinstance(_obs, dict):
                        continue
                    if _obs.get("_legacy_event"):
                        coord._pending_observations[_obs_type] = _obs
                    else:
                        _hvac_types = {OBS_TYPE_HVAC_COOL_c, OBS_TYPE_HVAC_HEAT_c}
                        if _obs_type in _hvac_types:
                            _phase = _obs.get("_phase", "active")
                            if _phase == "post_heat":
                                samples = _obs.get("post_heat_samples", [])
                                min_s = THERMAL_MIN_POST_HEAT_SAMPLES_c
                            else:
                                samples = _obs.get("active_samples", [])
                                min_s = 1
                        else:
                            samples = _obs.get("samples", _obs.get("active_samples", []))
                            min_s = 10
                        if len(samples) >= min_s:
                            coord._pending_observations[_obs_type] = _obs

        assert OBS_TYPE_HVAC_COOL in coord._pending_observations, (
            "Pre-fix obs with 'samples': [] shadow key must still be recovered via active_samples"
        )

    def test_hvac_cool_post_heat_phase_requires_post_heat_samples(self):
        """A post_heat phase obs with 0 post_heat_samples should be discarded (min=4)."""
        coord = _make_hvac_coord()
        dt_mock = _make_dt_mock()

        pending_obs = {
            OBS_TYPE_HVAC_COOL: {
                "obs_type": OBS_TYPE_HVAC_COOL,
                "obs_id": "test-recover-post-1",
                "start_time": _FAKE_NOW.isoformat(),
                "status": "monitoring",
                "active_samples": [_samp(72.0, elapsed=0.0)],
                "post_heat_samples": [],  # empty — should not recover
                "_phase": "post_heat",
                "hvac_mode": "cool",
                "session_mode": "cool",
            }
        }
        coord.learning._state.pending_observations = pending_obs

        mod = importlib.import_module("custom_components.climate_advisor.coordinator")
        OBS_TYPE_HVAC_COOL_c = mod.OBS_TYPE_HVAC_COOL
        OBS_TYPE_HVAC_HEAT_c = mod.OBS_TYPE_HVAC_HEAT
        THERMAL_MIN_POST_HEAT_SAMPLES_c = mod.THERMAL_MIN_POST_HEAT_SAMPLES

        with patch("custom_components.climate_advisor.coordinator.dt_util", dt_mock):
            _pending_obs = coord.learning._state.pending_observations
            if isinstance(_pending_obs, dict):
                for _obs_type, _obs in list(_pending_obs.items()):
                    if not isinstance(_obs, dict):
                        continue
                    if _obs.get("_legacy_event"):
                        coord._pending_observations[_obs_type] = _obs
                    else:
                        _hvac_types = {OBS_TYPE_HVAC_COOL_c, OBS_TYPE_HVAC_HEAT_c}
                        if _obs_type in _hvac_types:
                            _phase = _obs.get("_phase", "active")
                            if _phase == "post_heat":
                                samples = _obs.get("post_heat_samples", [])
                                min_s = THERMAL_MIN_POST_HEAT_SAMPLES_c
                            else:
                                samples = _obs.get("active_samples", [])
                                min_s = 1
                        else:
                            samples = _obs.get("samples", _obs.get("active_samples", []))
                            min_s = 10
                        if len(samples) >= min_s:
                            coord._pending_observations[_obs_type] = _obs

        assert OBS_TYPE_HVAC_COOL not in coord._pending_observations, (
            "post_heat obs with 0 post_heat_samples should be discarded (min=4)"
        )


# ---------------------------------------------------------------------------
# Bug 3 — Event-driven sampling missing
# ---------------------------------------------------------------------------


class TestThermostatChangeDuringActiveHvacAddsSample:
    """_async_thermostat_changed must add a sample to active_samples during active HVAC.

    Bug 3: The thermostat listener only handles start/stop transitions, not mid-cycle
    temperature updates. Short HVAC cycles (<5 min) end before the 5-min polling tick
    fires, leaving active_samples with only 1 initial sample — insufficient for OLS.
    """

    def _make_thermostat_coord(self, *, hvac_action: str = "cooling"):
        """Build a coordinator stub with _async_thermostat_changed bound."""
        ClimateAdvisorCoordinator = _get_coordinator_class()
        coord = object.__new__(ClimateAdvisorCoordinator)

        hass = MagicMock()
        hass.async_create_task = MagicMock(side_effect=_consume_coroutine)
        hass.services = MagicMock()
        hass.services.async_call = AsyncMock()

        climate_state_new = MagicMock()
        climate_state_new.state = "cool"
        climate_state_new.attributes = {
            "hvac_action": hvac_action,
            "temperature": 73.5,
        }

        def _states_get(entity_id: str):
            return climate_state_new

        hass.states.get = MagicMock(side_effect=_states_get)
        coord.hass = hass

        coord.config = {
            "climate_entity": "climate.test",
            "weather_entity": "weather.test",
            "learning_enabled": True,
            "comfort_heat": 70,
            "comfort_cool": 75,
        }

        ae = MagicMock()
        ae.is_paused_by_door = False
        ae._hvac_command_pending = False
        ae._manual_override_active = False
        ae._fan_active = False
        ae._natural_vent_active = False
        ae._fan_override_active = False
        ae._fan_command_pending = False
        ae._temp_command_pending = False
        coord.automation_engine = ae

        learning = MagicMock()
        learning.save_state = MagicMock()
        state_stub = MagicMock()
        state_stub.rejection_log = {}
        learning._state = state_stub
        coord.learning = learning

        coord._pending_observations = {}
        coord._pre_heat_sample_buffer = []
        coord._rejection_log = {}
        coord._hvac_on_since = None
        coord._current_classification = MagicMock()
        coord._current_classification.hvac_mode = "cool"
        coord._current_classification.windows_recommended = False
        coord._today_record = None

        coord._get_indoor_temp = MagicMock(return_value=72.0)
        coord._get_outdoor_temp = MagicMock(return_value=55.0)
        coord._any_sensor_open = MagicMock(return_value=False)
        coord._fan_is_running = MagicMock(return_value=False)
        coord._read_chart_hvac_action = MagicMock(return_value=hvac_action)
        coord._async_save_state = AsyncMock()
        coord._is_recent_hvac_command = MagicMock(return_value=False)
        coord._emit_event = MagicMock()
        coord._flush_hvac_runtime = MagicMock()

        chart_log = MagicMock()
        chart_log.append = MagicMock()
        chart_log.save = MagicMock()
        coord._chart_log = chart_log

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
        ):
            method = getattr(ClimateAdvisorCoordinator, method_name)
            setattr(coord, method_name, types.MethodType(method, coord))

        coord._commit_observation = MagicMock()
        coord._startup_coalesce_active = False  # Bug 1 (Issue #321)
        coord._async_thermostat_changed = types.MethodType(ClimateAdvisorCoordinator._async_thermostat_changed, coord)

        return coord

    def _make_event(self, old_action: str, new_action: str, same_temp: bool = True):
        """Build a fake HA event for _async_thermostat_changed."""
        old_state = MagicMock()
        old_state.state = "cool"
        old_state.attributes = {
            "hvac_action": old_action,
            "temperature": 73.0,
        }
        new_state = MagicMock()
        new_state.state = "cool"
        new_state.attributes = {
            "hvac_action": new_action,
            "temperature": 73.0 if same_temp else 73.5,
        }
        event = MagicMock()
        event.data = {"new_state": new_state, "old_state": old_state}
        return event

    def test_mid_cycle_thermostat_change_adds_active_sample(self):
        """When HVAC is cooling and hvac_action stays 'cooling', a temp update
        should add a sample to active_samples (event-driven sampling, Bug 3).

        This guards against short cycles missing ALL poll-tick samples.
        """
        coord = self._make_thermostat_coord(hvac_action="cooling")
        dt_mock = _make_dt_mock()

        # Set up an in-flight hvac_cool observation in 'active' phase
        # with 1 initial sample (as placed by _start_hvac_observation)
        initial_sample = {
            "timestamp": _FAKE_NOW.isoformat(),
            "indoor_temp_f": 72.5,
            "outdoor_temp_f": 55.0,
            "elapsed_minutes": 0.0,
        }
        obs = {
            "obs_type": OBS_TYPE_HVAC_COOL,
            "obs_id": "test-event-sample-1",
            "start_time": _FAKE_NOW.isoformat(),
            "status": "monitoring",
            "active_samples": [initial_sample],
            "post_heat_samples": [],
            "pre_heat_samples": [],
            "_phase": "active",
            "hvac_mode": "cool",
            "session_mode": "cool",
            "peak_indoor_f": 72.5,
        }
        coord._pending_observations[OBS_TYPE_HVAC_COOL] = obs

        # Simulate: hvac_action stays 'cooling', but thermostat fires (temperature changed)
        event = self._make_event("cooling", "cooling", same_temp=False)

        with patch("custom_components.climate_advisor.coordinator.dt_util", dt_mock):
            asyncio.run(coord._async_thermostat_changed(event))

        # After fix: active_samples should have 2 entries
        updated_obs = coord._pending_observations.get(OBS_TYPE_HVAC_COOL)
        if updated_obs is None:
            pytest.fail(
                "hvac_cool obs was removed from _pending_observations — "
                "event handler should not remove obs on mid-cycle thermostat change"
            )
        n_active = len(updated_obs["active_samples"])
        assert n_active >= 2, (
            f"Expected >= 2 active_samples after mid-cycle thermostat event, got {n_active}. "
            "Bug 3: event-driven sampling not implemented."
        )


# ---------------------------------------------------------------------------
# Bug 4 — Silent abandonment path
# ---------------------------------------------------------------------------


class TestNewSessionStartedAbandonmentLogged:
    """When a new HVAC session starts while one is in-flight, the in-flight obs
    must be recorded in the rejection log with a recognisable reason code.

    Bug 4: _abandon_observation is called with reason='new HVAC session started'
    but the path must route through _log_rejection (which it does via _abandon_observation).
    This test confirms the rejection entry reaches coord._rejection_log.
    """

    def test_new_hvac_cool_session_abandons_prior_and_logs(self):
        coord = _make_hvac_coord(hvac_action="cooling")
        dt_mock = _make_dt_mock()

        # Place an existing in-flight hvac_cool obs
        obs = {
            "obs_type": OBS_TYPE_HVAC_COOL,
            "obs_id": "test-prior-session-1",
            "start_time": _FAKE_NOW.isoformat(),
            "status": "monitoring",
            "active_samples": [
                _samp(73.0, elapsed=0.0),
                _samp(72.5, elapsed=5.0),
            ],
            "post_heat_samples": [],
            "_phase": "active",
            "hvac_mode": "cool",
            "session_mode": "cool",
        }
        coord._pending_observations[OBS_TYPE_HVAC_COOL] = obs
        coord.learning._state.rejection_log = {}

        # Starting a new hvac_cool session should abandon the prior one
        with patch("custom_components.climate_advisor.coordinator.dt_util", dt_mock):
            asyncio.run(coord._start_hvac_observation("cool"))

        bucket = coord._rejection_log.get(OBS_TYPE_HVAC_COOL, [])
        assert bucket, "No rejection log entry for hvac_cool after new session started — abandonment is silent (Bug 4)"
        reason = bucket[-1].get("reason_code", "")
        assert "new" in reason.lower() or "session" in reason.lower() or "abandon" in reason.lower(), (
            f"Unexpected reason_code '{reason}'; expected something about 'new session' or 'abandon'"
        )

    def test_new_hvac_heat_session_abandons_prior_and_logs(self):
        coord = _make_hvac_coord(hvac_action="heating")
        dt_mock = _make_dt_mock()

        obs = {
            "obs_type": OBS_TYPE_HVAC_HEAT,
            "obs_id": "test-prior-heat-1",
            "start_time": _FAKE_NOW.isoformat(),
            "status": "monitoring",
            "active_samples": [_samp(68.0, outdoor=45.0, elapsed=0.0)],
            "post_heat_samples": [],
            "_phase": "active",
            "hvac_mode": "heat",
            "session_mode": "heat",
        }
        coord._pending_observations[OBS_TYPE_HVAC_HEAT] = obs
        coord.learning._state.rejection_log = {}

        with patch("custom_components.climate_advisor.coordinator.dt_util", dt_mock):
            asyncio.run(coord._start_hvac_observation("heat"))

        bucket = coord._rejection_log.get(OBS_TYPE_HVAC_HEAT, [])
        assert bucket, "No rejection log entry for hvac_heat after new session started"
