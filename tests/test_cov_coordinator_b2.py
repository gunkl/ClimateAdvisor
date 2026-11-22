"""Coverage for the coordinator's observation sampler, observation commits and the pooled-cycle wiring (#1644).

What the person at home gets from this code: Climate Advisor watches the house while nothing runs (passive decay),
with the windows open and in the sun, and records each stretch as a learned rate only when it holds enough signal.
Heating and cooling cycles feed the cycle model. These tests check that a stretch is sampled, committed or dropped as
the rules say, that learning switched off records nothing, and that a recorder problem never breaks a state listener.
"""

from __future__ import annotations

import asyncio
import logging
import types
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.climate_advisor import coordinator as _coord_mod
from custom_components.climate_advisor.const import (
    OBS_TYPE_HVAC_HEAT,
    OBS_TYPE_PASSIVE_DECAY,
    OBS_TYPE_SOLAR_GAIN,
    OBS_TYPE_VENT_WINDOW_DECAY,
    THERMAL_MAX_OBS_SAMPLES,
    THERMAL_ROLLING_MAX_WINDOW_MINUTES,
    THERMAL_SOLAR_MIN_SAMPLES,
)
from tests.test_thermal_observations import _FAKE_NOW, _make_dt_mock, _make_obs_coord

ClimateAdvisorCoordinator = _coord_mod.ClimateAdvisorCoordinator
_LOG = "custom_components.climate_advisor.coordinator"
_DT = "custom_components.climate_advisor.coordinator.dt_util"


def _sample(ts, indoor, outdoor=55.0, elapsed=0.0):
    return {"timestamp": ts.isoformat(), "indoor_temp_f": indoor, "outdoor_temp_f": outdoor, "elapsed_minutes": elapsed}


def _obs(obs_type, *, start=None, samples=None, status="monitoring", **extra):
    o = {
        "obs_type": obs_type,
        "obs_id": f"t-{obs_type}",
        "status": status,
        "samples": samples if samples is not None else [],
        "flags_at_start": {},
        "schema_version": 1,
        **extra,
    }
    if start is not None:
        o["start_time"] = start.isoformat()
    return o


def _closing_spawn(coord):
    """Record what would be spawned and close it (never leak a coroutine)."""
    spawned = []

    def _spawn(coro, **_kw):
        spawned.append(coro.cr_code.co_name)
        coro.close()

    coord._spawn = _spawn
    return spawned


# ---------------------------------------------------------------------------
# _sample_all_observations
# ---------------------------------------------------------------------------


class TestSamplerGuards:
    def test_learning_off_starts_nothing(self):
        coord = _make_obs_coord(learning_enabled=False)
        with patch(_DT, _make_dt_mock()):
            coord._sample_all_observations()
        assert coord._pending_observations == {}

    def test_no_learning_engine_starts_nothing(self):
        coord = _make_obs_coord()
        del coord.learning
        with patch(_DT, _make_dt_mock()):
            coord._sample_all_observations()
        assert coord._pending_observations == {}

    def test_non_monitoring_observation_is_left_alone(self):
        coord = _make_obs_coord(indoor_temp=72.0, outdoor_temp=71.0)
        held = _obs(OBS_TYPE_VENT_WINDOW_DECAY, start=_FAKE_NOW, status="committing")
        coord._pending_observations[OBS_TYPE_VENT_WINDOW_DECAY] = held
        with patch(_DT, _make_dt_mock()):
            coord._sample_all_observations()
        assert coord._pending_observations[OBS_TYPE_VENT_WINDOW_DECAY] is held
        assert held["samples"] == []


class TestSamplerSampling:
    def test_hvac_run_with_unreadable_start_and_no_sample(self):
        coord = _make_obs_coord(hvac_action="heating", indoor_temp=68.0, outdoor_temp=40.0)
        coord._pending_observations[OBS_TYPE_HVAC_HEAT] = {
            "status": "monitoring",
            "active_start": "bad",
            "active_samples": [],
        }
        coord._get_current_sample = MagicMock(return_value=None)
        dt = _make_dt_mock()
        dt.parse_datetime.side_effect = ValueError("bad stamp")
        with patch(_DT, dt):
            coord._sample_all_observations()
        coord._get_current_sample.assert_called_once_with(0.0)  # an unreadable start reads as "now"
        assert coord._pending_observations[OBS_TYPE_HVAC_HEAT]["active_samples"] == []

    def test_decay_observation_start_stamp_variants(self):
        """Unreadable start → elapsed 0; no start → elapsed 0; a start that parses to None → elapsed 0."""
        coord = _make_obs_coord(indoor_temp=75.0, outdoor_temp=55.0, any_sensor_open=True)
        coord._get_current_sample = MagicMock(return_value=None)
        coord._pending_observations[OBS_TYPE_VENT_WINDOW_DECAY] = _obs(OBS_TYPE_VENT_WINDOW_DECAY)
        coord._evaluate_vent_split_observation = MagicMock()
        for start in ("raise", None, "none"):
            obs = coord._pending_observations[OBS_TYPE_VENT_WINDOW_DECAY]
            obs.pop("start_time", None)
            if start is not None:
                obs["start_time"] = start
            dt = _make_dt_mock()
            if start == "raise":
                dt.parse_datetime.side_effect = ValueError("bad")
            with patch(_DT, dt):
                coord._sample_all_observations()
        assert [c.args[0] for c in coord._get_current_sample.call_args_list] == [0.0, 0.0, 0.0]

    def test_full_sample_buffer_commits_instead_of_growing(self):
        coord = _make_obs_coord(indoor_temp=75.0, outdoor_temp=55.0, any_sensor_open=True)
        samples = [_sample(_FAKE_NOW, 75.0)] * THERMAL_MAX_OBS_SAMPLES
        coord._pending_observations[OBS_TYPE_VENT_WINDOW_DECAY] = _obs(
            OBS_TYPE_VENT_WINDOW_DECAY, start=_FAKE_NOW - timedelta(hours=1), samples=list(samples)
        )
        coord._commit_observation_if_sufficient = MagicMock()
        coord._evaluate_vent_split_observation = MagicMock()
        with patch(_DT, _make_dt_mock()):
            coord._sample_all_observations()
        coord._commit_observation_if_sufficient.assert_called_once_with(
            OBS_TYPE_VENT_WINDOW_DECAY, "max_samples_reached"
        )
        assert len(coord._pending_observations[OBS_TYPE_VENT_WINDOW_DECAY]["samples"]) == THERMAL_MAX_OBS_SAMPLES


class TestSamplerSolarGain:
    def _coord(self, **kw):
        coord = _make_obs_coord(**kw)
        coord._evaluate_rolling_window = MagicMock(return_value=False)
        coord._abandon_observation = MagicMock()
        coord._commit_observation_if_sufficient = MagicMock()
        return coord

    def test_heating_starts_abandons_sun_stretch(self):
        coord = self._coord(hvac_action="heating", indoor_temp=70.0, outdoor_temp=50.0)
        coord._pending_observations[OBS_TYPE_SOLAR_GAIN] = _obs(OBS_TYPE_SOLAR_GAIN, start=_FAKE_NOW)
        with patch(_DT, _make_dt_mock()):
            coord._sample_all_observations()
        coord._abandon_observation.assert_called_once_with(OBS_TYPE_SOLAR_GAIN, "hvac_started")

    def test_steady_sun_warming_is_committed(self):
        start = _FAKE_NOW - timedelta(hours=2)
        samples = [_sample(start + timedelta(minutes=6 * i), 68.0 + 0.1 * i) for i in range(THERMAL_SOLAR_MIN_SAMPLES)]
        coord = self._coord(indoor_temp=70.0, outdoor_temp=60.0)
        coord._pending_observations[OBS_TYPE_SOLAR_GAIN] = _obs(
            OBS_TYPE_SOLAR_GAIN, start=start, samples=samples, last_sample_time=_FAKE_NOW.isoformat()
        )
        with patch(_DT, _make_dt_mock()):
            coord._sample_all_observations()
        coord._commit_observation_if_sufficient.assert_called_once_with(OBS_TYPE_SOLAR_GAIN, "insufficient_rate")

    def test_first_sample_stamped_now_is_not_judged(self):
        """Elapsed time 0 since the first sample: no rate can be computed, the stretch keeps collecting."""
        samples = [_sample(_FAKE_NOW, 68.0 + 0.1 * i) for i in range(THERMAL_SOLAR_MIN_SAMPLES)]
        coord = self._coord(indoor_temp=70.0, outdoor_temp=60.0)
        coord._pending_observations[OBS_TYPE_SOLAR_GAIN] = _obs(
            OBS_TYPE_SOLAR_GAIN, start=_FAKE_NOW, samples=samples, last_sample_time=_FAKE_NOW.isoformat()
        )
        with patch(_DT, _make_dt_mock()):
            coord._sample_all_observations()
        coord._commit_observation_if_sufficient.assert_not_called()
        coord._abandon_observation.assert_not_called()


# ---------------------------------------------------------------------------
# Observation commits
# ---------------------------------------------------------------------------


class TestCommitIfSufficient:
    def test_missing_observation_is_a_no_op(self):
        coord = _make_obs_coord()
        coord._abandon_observation = MagicMock()
        coord._commit_observation_if_sufficient(OBS_TYPE_PASSIVE_DECAY, "why")
        coord._abandon_observation.assert_not_called()


class TestCommitObservation:
    def _coord(self, *, learning_enabled=True):
        coord = _make_obs_coord(learning_enabled=learning_enabled)
        coord._executor_job = AsyncMock(return_value=({"hvac_mode": "heat"}, None, 0.9))
        coord._today_record = types.SimpleNamespace(thermal_session_count=2)
        return coord

    def test_missing_observation_is_a_no_op(self):
        coord = self._coord()
        asyncio.run(coord._commit_observation(OBS_TYPE_PASSIVE_DECAY))
        coord._executor_job.assert_not_awaited()

    def test_learning_switched_off_drops_without_recording(self):
        coord = self._coord(learning_enabled=False)
        coord._pending_observations[OBS_TYPE_PASSIVE_DECAY] = _obs(OBS_TYPE_PASSIVE_DECAY)
        asyncio.run(coord._commit_observation(OBS_TYPE_PASSIVE_DECAY))
        assert OBS_TYPE_PASSIVE_DECAY not in coord._pending_observations
        coord._executor_job.assert_awaited_once_with(coord.learning.save_state)

    def test_hvac_commit_counts_a_thermal_session(self):
        coord = self._coord()
        coord._pending_observations[OBS_TYPE_HVAC_HEAT] = _obs(OBS_TYPE_HVAC_HEAT)
        asyncio.run(coord._commit_observation(OBS_TYPE_HVAC_HEAT, force_grade="low"))
        assert coord._today_record.thermal_session_count == 3
        assert OBS_TYPE_HVAC_HEAT not in coord._pending_observations


class TestRollingWindow:
    def _coord(self):
        coord = _make_obs_coord()
        coord._abandon_observation = MagicMock()
        spawned = _closing_spawn(coord)
        coord._pending_observations[OBS_TYPE_SOLAR_GAIN] = {}
        return coord, spawned

    def test_unreadable_start_keeps_collecting(self):
        coord, _ = self._coord()
        dt = _make_dt_mock()
        dt.parse_datetime.side_effect = ValueError("bad")
        with patch(_DT, dt):
            assert coord._evaluate_rolling_window(OBS_TYPE_SOLAR_GAIN, {"start_time": "x"}, False) is False

    def test_start_parsing_to_none_reads_as_just_started(self):
        coord, _ = self._coord()
        with patch(_DT, _make_dt_mock()):
            assert coord._evaluate_rolling_window(OBS_TYPE_SOLAR_GAIN, {"start_time": "garbage"}, False) is False

    def test_long_weak_window_is_trimmed_and_kept(self, caplog):
        coord, _ = self._coord()
        start = _FAKE_NOW - timedelta(minutes=90)
        obs = {"start_time": start.isoformat(), "samples": [_sample(_FAKE_NOW, 70.0) for _ in range(120)]}
        with patch(_DT, _make_dt_mock()), caplog.at_level(logging.INFO, logger=_LOG):
            assert coord._evaluate_rolling_window(OBS_TYPE_SOLAR_GAIN, obs, False) is False
        assert len(obs["samples"]) == 96
        assert "keeping alive" in caplog.text

    def test_too_few_samples_at_commit_abandons(self):
        coord, spawned = self._coord()
        obs = {"start_time": (_FAKE_NOW - timedelta(minutes=40)).isoformat(), "samples": [_sample(_FAKE_NOW, 70.0)]}
        with patch(_DT, _make_dt_mock()):
            assert coord._evaluate_rolling_window(OBS_TYPE_SOLAR_GAIN, obs, True) is True
        coord._abandon_observation.assert_called_once_with(OBS_TYPE_SOLAR_GAIN, "window_elapsed_too_few_samples")
        assert spawned == []

    def test_flat_window_at_commit_is_dropped(self, caplog):
        coord, spawned = self._coord()
        obs = {
            "start_time": (_FAKE_NOW - timedelta(minutes=40)).isoformat(),
            "samples": [_sample(_FAKE_NOW, 70.0) for _ in range(6)],
        }
        with patch(_DT, _make_dt_mock()), caplog.at_level(logging.INFO, logger=_LOG):
            assert coord._evaluate_rolling_window(OBS_TYPE_SOLAR_GAIN, obs, True) is True
        assert OBS_TYPE_SOLAR_GAIN not in coord._pending_observations
        assert "insufficient total ΔT" in caplog.text
        assert spawned == []

    def test_hard_cap_commits_without_delta_guard(self):
        coord, spawned = self._coord()
        obs = {
            "start_time": (_FAKE_NOW - timedelta(minutes=THERMAL_ROLLING_MAX_WINDOW_MINUTES + 5)).isoformat(),
            "samples": [_sample(_FAKE_NOW, 70.0) for _ in range(6)],
        }
        with patch(_DT, _make_dt_mock()):
            assert coord._evaluate_rolling_window(OBS_TYPE_SOLAR_GAIN, obs, False) is True
        assert obs["status"] == "committing"
        assert spawned == ["_commit_observation"]


# ---------------------------------------------------------------------------
# Pooled cycle recorder wiring
# ---------------------------------------------------------------------------


def _cycle_coord(*, recorder=True, learning_enabled=True):
    coord = object.__new__(ClimateAdvisorCoordinator)
    coord.config = {"learning_enabled": learning_enabled, "climate_entity": "climate.t"}
    coord._cycle_recorder = MagicMock() if recorder else None
    for name in (
        "_cycles_enabled",
        "_cycle_primary_is_v3",
        "_cycle_collect",
        "_cycle_flags",
        "_cycle_reading",
        "_cycle_tick",
        "_cycle_gap",
        "_cycle_sync_companion_listener",
        "_cycle_unsub_companion",
        "_cycle_process",
    ):
        setattr(coord, name, types.MethodType(getattr(ClimateAdvisorCoordinator, name), coord))
    return coord


class TestCycleWiring:
    def test_primary_engine_lookup_failure_reads_as_not_v3(self):
        coord = _cycle_coord()
        with patch.object(_coord_mod.prediction_engines, "primary_id", side_effect=KeyError("x")):
            assert coord._cycle_primary_is_v3() is False

    def test_disabled_recorder_records_nothing(self):
        for coord in (_cycle_coord(recorder=False), _cycle_coord(learning_enabled=False)):
            coord._cycle_collect = MagicMock()
            coord._cycle_flags()
            coord._cycle_reading()
            coord._cycle_tick()
            coord._cycle_gap("restart")
            coord._cycle_sync_companion_listener()
            coord._cycle_collect.assert_not_called()

    def test_recorder_failures_never_escape(self, caplog):
        coord = _cycle_coord()
        coord._cycle_flag_state = MagicMock(side_effect=RuntimeError("flags"))
        coord._primary_indoor_entity = MagicMock(side_effect=RuntimeError("entity"))
        coord._cycle_recorder.gap.side_effect = RuntimeError("gap")
        coord._cycle_outdoor = MagicMock(side_effect=RuntimeError("outdoor"))
        with caplog.at_level(logging.DEBUG, logger=_LOG):
            coord._cycle_flags()
            coord._cycle_reading("sensor.x")
            coord._cycle_tick()
            coord._cycle_gap("unavailable")
        for msg in ("flags failed", "reading failed", "tick failed", "gap failed"):
            assert f"cycle recorder {msg}" in caplog.text

    def test_unscheduled_processing_is_dropped_cleanly(self, caplog):
        coord = _cycle_coord()
        coord._spawn = MagicMock(side_effect=RuntimeError("no loop"))
        with caplog.at_level(logging.DEBUG, logger=_LOG):
            coord._cycle_collect([{"mode": "heat"}])
        assert "cycle processing not scheduled (1 closed cycle(s) dropped)" in caplog.text

    def test_companion_listener_subscribes_and_survives_errors(self, caplog):
        coord = _cycle_coord()
        coord.hass = MagicMock()
        coord._cycle_companion_wanted = MagicMock(return_value=("sensor.a",))
        coord._zone_scoped_sync = lambda fn: fn
        unsub = MagicMock()
        with patch.object(_coord_mod, "async_track_state_change_event", return_value=unsub) as track:
            coord._cycle_sync_companion_listener()
        assert track.call_args.args[1] == ["sensor.a"]
        assert coord._cycle_companion_unsub is unsub
        assert coord._cycle_companion_entities == ("sensor.a",)
        # shut down: no resubscribe
        coord._shut_down = True
        coord._cycle_companion_wanted.return_value = ("sensor.b",)
        coord._cycle_sync_companion_listener()
        assert coord._cycle_companion_entities == ("sensor.a",)
        # a lookup failure is logged, never raised
        coord._shut_down = False
        coord._cycle_companion_wanted.side_effect = RuntimeError("boom")
        with caplog.at_level(logging.DEBUG, logger=_LOG):
            coord._cycle_sync_companion_listener()
        assert "companion listener sync failed" in caplog.text


class TestCycleProcess:
    def _coord(self):
        coord = _cycle_coord()
        _lock = asyncio.Lock()
        coord._cycle_lock = lambda: _lock
        coord._cycle_store = MagicMock()
        coord._cycle_judge = AsyncMock(return_value=1)
        coord._cycle_refit_due_modes = MagicMock(return_value=[])
        coord._cycle_refit = AsyncMock()
        coord._cycle_save_due = MagicMock(return_value=False)
        coord._executor_job = AsyncMock(return_value=True)
        coord.learning = MagicMock()
        return coord

    def test_fan_cycles_are_ignored_and_bare_coordinator_gets_pending(self):
        coord = self._coord()
        asyncio.run(coord._cycle_process([{"mode": "fan"}, {"mode": "heat"}]))
        coord._cycle_store.add.assert_called_once_with({"mode": "heat"})
        assert coord._cycle_pending == {"heat": {"n": 1, "eligible": 1}}
        coord._executor_job.assert_not_awaited()

    def test_failure_keeps_previous_rates(self, caplog):
        coord = self._coord()
        coord._cycle_judge.side_effect = RuntimeError("fit")
        with caplog.at_level(logging.WARNING, logger=_LOG):
            asyncio.run(coord._cycle_process([{"mode": "cool"}]))
        assert "Cycle model update failed" in caplog.text

    def test_cancellation_propagates(self):
        coord = self._coord()
        coord._cycle_judge.side_effect = asyncio.CancelledError()
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(coord._cycle_process([{"mode": "cool"}]))
