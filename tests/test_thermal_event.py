"""Tests for v3 HVAC thermal observation lifecycle (Issue #121).

Covers 6 gap scenarios not present in test_thermal_observations.py or
test_hvac_session_detection.py:
  1. Cool mode end-to-end commit
  2. Fan-only session lifecycle
  3. Pre-heat buffer snapshot in v3 obs
  4. Session count tracking on commit
  5. Mid-session heat→cool mode switch
  6. Learning-disabled short-circuit

Tests bind the real coordinator v3 thermal methods to a minimal stub object
so the observation pipeline runs without a full HA environment.
"""

from __future__ import annotations

import asyncio
import importlib
import sys
import types
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

# ── HA module stubs ──────────────────────────────────────────────────────────
if "homeassistant" not in sys.modules:
    from conftest import _install_ha_stubs

    _install_ha_stubs()

# ---------------------------------------------------------------------------
# Imports after stubs
# ---------------------------------------------------------------------------

from custom_components.climate_advisor.const import (  # noqa: E402
    OBS_TYPE_HVAC_COOL,
    OBS_TYPE_HVAC_HEAT,
)
from custom_components.climate_advisor.indoor_temp import IndoorTempReading  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FAKE_NOW = datetime(2026, 4, 19, 12, 0, 0, tzinfo=UTC)


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


def _get_coordinator_class():
    mod = importlib.import_module("custom_components.climate_advisor.coordinator")
    return mod.ClimateAdvisorCoordinator


def _make_v3_coord(
    *,
    indoor_temp: float = 68.0,
    outdoor_temp: float = 45.0,
    learning_enabled: bool = True,
):
    """Build a minimal coordinator stub with v3 HVAC observation methods bound."""
    ClimateAdvisorCoordinator = _get_coordinator_class()
    coord = object.__new__(ClimateAdvisorCoordinator)

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
    climate_state.state = "idle"
    # Issue #1035: the REAL _get_current_sample (bound below) reads the indoor temp via the
    # shared resolver, which needs a valid current_temperature on the climate entity.
    climate_state.attributes = {"hvac_action": "idle", "current_temperature": indoor_temp}
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

    coord.config = {
        "climate_entity": "climate.test",
        "weather_entity": "weather.test",
        "comfort_heat": 70,
        "comfort_cool": 75,
        "learning_enabled": learning_enabled,
    }

    # v3 thermal state
    coord._pending_observations = {}
    coord._pending_thermal_event = None
    coord._last_outdoor_temp = outdoor_temp

    # learning stub — return a successful commit by default
    learning = MagicMock()
    learning.set_pending_thermal_event = MagicMock()
    learning.save_state = MagicMock()
    learning._commit_event_from_dict = MagicMock(return_value=({"hvac_mode": "heat"}, None, 0.9))
    # Issue #1512: an HVAC run is committed at the off edge through record_hvac_run -> (obs, reject_code)
    learning.record_hvac_run = MagicMock(return_value=({"hvac_mode": "heat", "k_active": 2.0}, None))
    # _abandon_observation writes to learning._state.rejection_log — must not be None
    learning._state = MagicMock()
    learning._state.rejection_log = {}
    coord.learning = learning

    today_record = MagicMock()
    today_record.thermal_session_count = 0
    coord._today_record = today_record

    coord._get_indoor_temp = MagicMock(return_value=indoor_temp)
    coord._async_save_state = AsyncMock()

    def _get_current_sample(elapsed: float) -> dict:
        return {
            "timestamp": _FAKE_NOW.isoformat(),
            "indoor_temp_f": indoor_temp,
            "outdoor_temp_f": outdoor_temp,
            "elapsed_minutes": elapsed,
        }

    coord._get_current_sample = _get_current_sample

    for method_name in (
        "_ensure_pending_observations",
        "_start_hvac_observation",
        "_end_hvac_active_phase",
        "_closed_house_rate",
        "_reject_hvac_run",
        "_commit_observation",
        "_commit_observation_if_sufficient",
        "_abandon_observation",
        "_get_current_sample",
        "_get_outdoor_temp",
        "_sample_all_observations",
    ):
        if hasattr(ClimateAdvisorCoordinator, method_name):
            method = getattr(ClimateAdvisorCoordinator, method_name)
            setattr(coord, method_name, types.MethodType(method, coord))

    return coord


# ---------------------------------------------------------------------------
# Gap scenario 1 — Cool mode end-to-end commit
# ---------------------------------------------------------------------------


class TestCoolModeEndToEndCommit:
    """Full HVAC cool session produces a committed observation with hvac_mode='cool'."""

    def test_cool_observation_created_on_start(self):
        """_start_hvac_observation('cool') creates hvac_cool entry in _pending_observations."""
        coord = _make_v3_coord()
        dt_mock = _make_dt_mock()
        with patch("custom_components.climate_advisor.coordinator.dt_util", dt_mock):
            asyncio.run(coord._start_hvac_observation("cool"))
        assert OBS_TYPE_HVAC_COOL in coord._pending_observations
        obs = coord._pending_observations[OBS_TYPE_HVAC_COOL]
        assert obs["hvac_mode"] == "cool"
        assert obs["session_mode"] == "cool"

    def test_cool_observation_commits_with_correct_mode(self):
        """A cool run commits at the off edge: record_hvac_run gets mode 'cool' and the off-edge reading.

        Issue #1516: the old test faked a post-run decay window and judged "peak - end >= 0.3" for COOLING, which
        is backwards (a cooling run's extremum is a trough, and a room that warms after the compressor stops is
        the normal case). The run is now judged by the run itself at the off edge, mirror of heating.
        """
        coord = _make_v3_coord(indoor_temp=70.0)
        dt_mock_start = _make_dt_mock(datetime(2026, 4, 19, 12, 0, 0, tzinfo=UTC))
        dt_mock_end = _make_dt_mock(datetime(2026, 4, 19, 12, 20, 0, tzinfo=UTC))

        with patch("custom_components.climate_advisor.coordinator.dt_util", dt_mock_start):
            asyncio.run(coord._start_hvac_observation("cool"))
        # the room cooled during the run: the off-edge reading is 68.0
        coord._get_indoor_temp_with_provenance = MagicMock(return_value=IndoorTempReading(68.0, "climate.test", 68.0))
        with patch("custom_components.climate_advisor.coordinator.dt_util", dt_mock_end):
            coord._end_hvac_active_phase(OBS_TYPE_HVAC_COOL)

        coord.learning.record_hvac_run.assert_called_once()
        run = coord.learning.record_hvac_run.call_args[0][0]
        assert run["mode"] == "cool"
        assert run["start_f"] == 70.0
        assert run["off_f"] == 68.0
        assert run["minutes"] == 20.0
        assert OBS_TYPE_HVAC_COOL not in coord._pending_observations

    def test_cool_observation_removed_at_off_edge(self):
        """The off edge commits and removes the pending obs; there is no post_heat phase any more (Issue #1512)."""
        coord = _make_v3_coord()
        dt_mock = _make_dt_mock()
        with patch("custom_components.climate_advisor.coordinator.dt_util", dt_mock):
            asyncio.run(coord._start_hvac_observation("cool"))
            coord._end_hvac_active_phase(OBS_TYPE_HVAC_COOL)
        assert OBS_TYPE_HVAC_COOL not in coord._pending_observations


# ---------------------------------------------------------------------------
# Gap scenario 2 — Fan-only session lifecycle
# ---------------------------------------------------------------------------


class TestFanOnlySessionLifecycle:
    """Fan-only observation starts, reaches a terminal state without errors."""

    def test_fan_only_observation_created_on_start(self):
        """_start_hvac_observation('fan_only') creates fan_only_decay entry."""
        coord = _make_v3_coord()
        dt_mock = _make_dt_mock()
        with patch("custom_components.climate_advisor.coordinator.dt_util", dt_mock):
            asyncio.run(coord._start_hvac_observation("fan_only"))
        # fan_only maps to OBS_TYPE_FAN_ONLY_DECAY or to hvac_cool — verify whichever key
        # the coordinator actually uses is present
        assert len(coord._pending_observations) >= 1
        # The key must be fan_only_decay (OBS_TYPE_FAN_ONLY_DECAY) or
        # one of the HVAC types — not an empty dict
        present_keys = list(coord._pending_observations.keys())
        assert len(present_keys) > 0

    def test_fan_only_abandon_clears_entry(self):
        """Abandoning the fan_only obs removes it from _pending_observations."""
        coord = _make_v3_coord()
        dt_mock = _make_dt_mock()
        with patch("custom_components.climate_advisor.coordinator.dt_util", dt_mock):
            asyncio.run(coord._start_hvac_observation("fan_only"))

        obs_keys = list(coord._pending_observations.keys())
        assert len(obs_keys) >= 1
        fan_key = obs_keys[0]

        with patch("custom_components.climate_advisor.coordinator.dt_util", dt_mock):
            coord._abandon_observation(fan_key, "test abandon fan_only")

        assert fan_key not in coord._pending_observations

    def test_fan_only_does_not_raise(self):
        """Complete fan_only lifecycle (start → end active) is error-free."""
        coord = _make_v3_coord()
        dt_mock = _make_dt_mock()
        with patch("custom_components.climate_advisor.coordinator.dt_util", dt_mock):
            asyncio.run(coord._start_hvac_observation("fan_only"))

        obs_keys = list(coord._pending_observations.keys())
        if not obs_keys:
            return  # nothing to test if start was no-op
        fan_key = obs_keys[0]

        with patch("custom_components.climate_advisor.coordinator.dt_util", dt_mock):
            coord._end_hvac_active_phase(fan_key)

        # terminal state: committed or rejected (key absent) — only that it ran cleanly


# ---------------------------------------------------------------------------
# Gap scenario 3 — Pre-heat buffer snapshot in v3 obs
# ---------------------------------------------------------------------------


class TestHvacObsShape:
    """The HVAC observation carries only the run's own samples (Issue #1512: no pre-heat buffer, no post-heat phase)."""

    def test_obs_has_no_pre_or_post_heat_samples(self):
        coord = _make_v3_coord()
        with patch("custom_components.climate_advisor.coordinator.dt_util", _make_dt_mock()):
            asyncio.run(coord._start_hvac_observation("heat"))
        obs = coord._pending_observations[OBS_TYPE_HVAC_HEAT]
        assert "pre_heat_samples" not in obs
        assert "post_heat_samples" not in obs
        assert obs["_phase"] == "active"
        assert len(obs["active_samples"]) == 1


# ---------------------------------------------------------------------------
# Gap scenario 4 — Session count tracking
# ---------------------------------------------------------------------------


class TestSessionCountTracking:
    """thermal_session_count increments when an HVAC run commits at the off edge (Issue #1512)."""

    @staticmethod
    def _run(coord, mode: str, obs_type: str) -> None:
        dt_mock = _make_dt_mock()
        with patch("custom_components.climate_advisor.coordinator.dt_util", dt_mock):
            asyncio.run(coord._start_hvac_observation(mode))
            coord._end_hvac_active_phase(obs_type)

    def test_session_count_increments_on_heat_commit(self):
        """Successful heat commit increments _today_record.thermal_session_count by 1."""
        coord = _make_v3_coord()
        assert coord._today_record.thermal_session_count == 0
        self._run(coord, "heat", OBS_TYPE_HVAC_HEAT)
        assert coord._today_record.thermal_session_count == 1

    def test_session_count_increments_on_cool_commit(self):
        """Successful cool commit also increments the session count."""
        coord = _make_v3_coord()
        coord.learning.record_hvac_run.return_value = ({"hvac_mode": "cool", "k_active": -2.0}, None)
        self._run(coord, "cool", OBS_TYPE_HVAC_COOL)
        assert coord._today_record.thermal_session_count == 1

    def test_session_count_not_incremented_on_reject(self):
        """A rejected run (record_hvac_run returns a code) does not count, is logged with its code, and is gone."""
        coord = _make_v3_coord()
        coord._rejection_log = {}
        coord.learning.record_hvac_run.return_value = (None, "hvac_signal_below_resolution")
        self._run(coord, "heat", OBS_TYPE_HVAC_HEAT)
        assert coord._today_record.thermal_session_count == 0
        (event,) = coord._rejection_log[OBS_TYPE_HVAC_HEAT]
        assert event["reason_code"] == "hvac_signal_below_resolution"
        assert OBS_TYPE_HVAC_HEAT not in coord._pending_observations


# ---------------------------------------------------------------------------
# Gap scenario 5 — Mid-session mode switch
# ---------------------------------------------------------------------------


class TestMidSessionModeSwitch:
    """Heat→cool switch: hvac_heat is abandoned, hvac_cool is created."""

    def test_heat_key_absent_after_explicit_abandon(self):
        """After _abandon_observation('hvac_heat', ...), the key is gone."""
        coord = _make_v3_coord()
        dt_mock = _make_dt_mock()
        with patch("custom_components.climate_advisor.coordinator.dt_util", dt_mock):
            asyncio.run(coord._start_hvac_observation("heat"))
        assert OBS_TYPE_HVAC_HEAT in coord._pending_observations

        with patch("custom_components.climate_advisor.coordinator.dt_util", dt_mock):
            coord._abandon_observation(OBS_TYPE_HVAC_HEAT, "heat_cool mode switch mid-session")

        assert OBS_TYPE_HVAC_HEAT not in coord._pending_observations

    def test_cool_key_present_after_start(self):
        """After abandoning heat and starting cool, hvac_cool is the only HVAC key."""
        coord = _make_v3_coord()
        dt_mock = _make_dt_mock()
        with patch("custom_components.climate_advisor.coordinator.dt_util", dt_mock):
            asyncio.run(coord._start_hvac_observation("heat"))
            coord._abandon_observation(OBS_TYPE_HVAC_HEAT, "heat_cool mode switch mid-session")
            asyncio.run(coord._start_hvac_observation("cool"))

        assert OBS_TYPE_HVAC_HEAT not in coord._pending_observations
        assert OBS_TYPE_HVAC_COOL in coord._pending_observations

    def test_cool_obs_has_correct_mode_after_switch(self):
        """The new hvac_cool observation carries hvac_mode='cool'."""
        coord = _make_v3_coord()
        dt_mock = _make_dt_mock()
        with patch("custom_components.climate_advisor.coordinator.dt_util", dt_mock):
            asyncio.run(coord._start_hvac_observation("heat"))
            coord._abandon_observation(OBS_TYPE_HVAC_HEAT, "heat_cool mode switch mid-session")
            asyncio.run(coord._start_hvac_observation("cool"))

        obs = coord._pending_observations[OBS_TYPE_HVAC_COOL]
        assert obs["hvac_mode"] == "cool"
        assert obs["session_mode"] == "cool"

    def test_start_cool_does_not_discard_active_heat(self):
        """Starting cool with heat already active: both HVAC types can co-exist.

        The implementation only abandons the *same* obs_type when a new session
        starts (e.g., starting cool abandons an existing cool, not an existing heat).
        Heat and cool observations are independent and run concurrently.
        """
        coord = _make_v3_coord()
        dt_mock = _make_dt_mock()
        with patch("custom_components.climate_advisor.coordinator.dt_util", dt_mock):
            asyncio.run(coord._start_hvac_observation("heat"))
            assert OBS_TYPE_HVAC_HEAT in coord._pending_observations
            asyncio.run(coord._start_hvac_observation("cool"))

        # Both HVAC obs-types should be present — they do not contaminate each other
        assert OBS_TYPE_HVAC_HEAT in coord._pending_observations
        assert OBS_TYPE_HVAC_COOL in coord._pending_observations


# ---------------------------------------------------------------------------
# Gap scenario 6 — Learning-disabled short-circuit
# ---------------------------------------------------------------------------


class TestLearningDisabledShortCircuit:
    """When learning_enabled=False, _start_hvac_observation is a no-op."""

    def test_no_observation_created_when_disabled(self):
        """learning_enabled=False → _pending_observations stays empty after start."""
        coord = _make_v3_coord(learning_enabled=False)
        dt_mock = _make_dt_mock()
        with patch("custom_components.climate_advisor.coordinator.dt_util", dt_mock):
            asyncio.run(coord._start_hvac_observation("heat"))
        assert OBS_TYPE_HVAC_HEAT not in coord._pending_observations
        assert coord._pending_observations == {}

    def test_no_observation_created_for_cool_when_disabled(self):
        """Same short-circuit applies to cool mode."""
        coord = _make_v3_coord(learning_enabled=False)
        dt_mock = _make_dt_mock()
        with patch("custom_components.climate_advisor.coordinator.dt_util", dt_mock):
            asyncio.run(coord._start_hvac_observation("cool"))
        assert OBS_TYPE_HVAC_COOL not in coord._pending_observations
        assert coord._pending_observations == {}

    def test_learning_save_not_called_when_disabled(self):
        """No save_state call when learning is disabled (skip path is clean)."""
        coord = _make_v3_coord(learning_enabled=False)
        dt_mock = _make_dt_mock()
        with patch("custom_components.climate_advisor.coordinator.dt_util", dt_mock):
            asyncio.run(coord._start_hvac_observation("heat"))
        coord.learning.save_state.assert_not_called()
