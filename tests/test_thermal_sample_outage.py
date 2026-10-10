"""Issue #1035: thermal learning must never see a fake 0.0 sample during an indoor outage.

Exercises the REAL coordinator methods (bound onto partial stubs) — never a copy of the logic:
  - ``_get_current_sample`` returns None when indoor is unreadable
  - ``_update_pre_heat_buffer`` skips on None but still prunes stale entries
  - ``_start_hvac_observation`` refuses to start (INFO + structured ``indoor_unavailable`` rejection)
  - the event-driven sample in ``_async_thermostat_changed`` skips on None
  - ``_build_learning_health`` / AI context register the new reason code
"""

from __future__ import annotations

import asyncio
import logging
import sys
from datetime import timedelta
from unittest.mock import MagicMock, patch

if "homeassistant" not in sys.modules:
    from conftest import _install_ha_stubs

    _install_ha_stubs()

if "anthropic" not in sys.modules:
    _mock_anthropic = MagicMock()
    _mock_anthropic.__name__ = "anthropic"
    _mock_anthropic.__path__ = []
    _mock_anthropic.__file__ = None
    _mock_anthropic.__spec__ = None
    _mock_anthropic.__loader__ = None
    _mock_anthropic.__package__ = "anthropic"
    _mock_anthropic.APIError = type("APIError", (Exception,), {})
    _mock_anthropic.APITimeoutError = type("APITimeoutError", (Exception,), {})
    _mock_anthropic.RateLimitError = type("RateLimitError", (Exception,), {})
    _mock_anthropic.NotFoundError = type("NotFoundError", (_mock_anthropic.APIError,), {})
    _mock_anthropic.AsyncAnthropic = MagicMock()
    sys.modules["anthropic"] = _mock_anthropic

import types  # noqa: E402

from custom_components.climate_advisor.ai_skills_investigator import (  # noqa: E402
    async_build_investigator_context,
)
from custom_components.climate_advisor.const import (  # noqa: E402
    OBS_TYPE_HVAC_COOL,
    OBS_TYPE_HVAC_HEAT,
    REJECT_ABANDONED,
    REJECT_INDOOR_UNAVAILABLE,
)
from tests.test_hvac_session_detection import (  # noqa: E402
    _get_coordinator_class,
    _make_state,
    _make_thermostat_coord,
    _make_thermostat_event,
)
from tests.test_investigator_thermal_health import (  # noqa: E402
    _make_coordinator as _make_investigator_coord,
)
from tests.test_investigator_thermal_health import (  # noqa: E402
    _make_hass as _make_investigator_hass,
)
from tests.test_investigator_thermal_health import (  # noqa: E402
    _make_learning_health,
)
from tests.test_thermal_event import _FAKE_NOW, _make_dt_mock, _make_v3_coord  # noqa: E402

_COORD_MOD = "custom_components.climate_advisor.coordinator"


def _blank_indoor(coord) -> None:
    """Make the climate entity's current_temperature unreadable (indoor sensor outage)."""
    coord.hass.states.get("climate.test").attributes["current_temperature"] = None


def _make_outage_coord(**kwargs):
    coord = _make_v3_coord(**kwargs)
    coord._rejection_log = {}
    return coord


class TestGetCurrentSample:
    def test_returns_none_when_indoor_unreadable(self):
        coord = _make_outage_coord()
        _blank_indoor(coord)
        with patch(f"{_COORD_MOD}.dt_util", _make_dt_mock()):
            assert coord._get_current_sample(0.0) is None

    def test_returns_dict_with_real_values_when_readable(self):
        coord = _make_outage_coord(indoor_temp=68.0, outdoor_temp=45.0)
        with patch(f"{_COORD_MOD}.dt_util", _make_dt_mock()):
            sample = coord._get_current_sample(3.0)
        assert sample is not None
        assert sample["indoor_temp_f"] == 68.0
        assert sample["outdoor_temp_f"] == 45.0
        assert sample["elapsed_minutes"] == 3.0


class TestPreHeatBuffer:
    def test_no_append_on_none_and_stale_entries_pruned(self, caplog):
        coord = _make_outage_coord()
        _blank_indoor(coord)
        stale = (_FAKE_NOW - timedelta(hours=3)).isoformat()
        fresh = (_FAKE_NOW - timedelta(minutes=1)).isoformat()
        coord._pre_heat_sample_buffer = [
            {"timestamp": stale, "indoor_temp_f": 60.0, "outdoor_temp_f": 40.0, "elapsed_minutes": 0.0},
            {"timestamp": fresh, "indoor_temp_f": 61.0, "outdoor_temp_f": 40.0, "elapsed_minutes": 0.0},
        ]
        with (
            patch(f"{_COORD_MOD}.dt_util", _make_dt_mock()),
            caplog.at_level(logging.DEBUG, logger=_COORD_MOD),
        ):
            coord._update_pre_heat_buffer()
        assert [s["timestamp"] for s in coord._pre_heat_sample_buffer] == [fresh]
        assert all(s["indoor_temp_f"] != 0.0 for s in coord._pre_heat_sample_buffer)
        skip = [r for r in caplog.records if "Pre-heat sample skipped" in r.getMessage()]
        assert len(skip) == 1
        assert skip[0].levelno == logging.DEBUG
        assert "indoor_unavailable" in skip[0].getMessage()

    def test_appends_when_readable(self):
        coord = _make_outage_coord(indoor_temp=67.5)
        with patch(f"{_COORD_MOD}.dt_util", _make_dt_mock()):
            coord._update_pre_heat_buffer()
        assert len(coord._pre_heat_sample_buffer) == 1
        assert coord._pre_heat_sample_buffer[0]["indoor_temp_f"] == 67.5


class TestStartHvacObservation:
    def test_indoor_none_does_not_start_and_records_rejection(self, caplog):
        coord = _make_outage_coord()
        _blank_indoor(coord)
        with (
            patch(f"{_COORD_MOD}.dt_util", _make_dt_mock()),
            caplog.at_level(logging.INFO, logger=_COORD_MOD),
        ):
            asyncio.run(coord._start_hvac_observation("heat"))
        assert coord._pending_observations == {}
        # The only save_state is the awaited persistence of the rejection entry,
        # never an observation-creation save: exactly one call, and no pending obs exists.
        assert coord.learning.save_state.call_count == 1
        entries = coord._rejection_log[OBS_TYPE_HVAC_HEAT]
        assert len(entries) == 1
        assert entries[0]["reason_code"] == REJECT_INDOOR_UNAVAILABLE
        assert entries[0]["obs_type"] == OBS_TYPE_HVAC_HEAT
        assert entries[0]["n_samples"] == 0
        assert coord.learning._state.rejection_log is coord._rejection_log
        info = [r for r in caplog.records if r.levelno == logging.INFO and "not started" in r.getMessage()]
        assert len(info) == 1
        assert "Thermal" in info[0].getMessage()
        assert "indoor_unavailable" in info[0].getMessage()
        # The skip must not be preceded by a misleading "starting" line.
        assert not [r for r in caplog.records if "starting" in r.getMessage()]

    def test_cool_mode_records_under_cool_type(self):
        coord = _make_outage_coord()
        _blank_indoor(coord)
        with patch(f"{_COORD_MOD}.dt_util", _make_dt_mock()):
            asyncio.run(coord._start_hvac_observation("cool"))
        assert list(coord._rejection_log) == [OBS_TYPE_HVAC_COOL]

    def test_indoor_available_still_starts_with_real_value(self, caplog):
        coord = _make_outage_coord(indoor_temp=66.5, outdoor_temp=41.0)
        with (
            patch(f"{_COORD_MOD}.dt_util", _make_dt_mock()),
            caplog.at_level(logging.INFO, logger=_COORD_MOD),
        ):
            asyncio.run(coord._start_hvac_observation("heat"))
        assert [
            r for r in caplog.records if "_start_hvac_observation" in r.getMessage() and "starting" in r.getMessage()
        ]
        assert not [r for r in caplog.records if "not started" in r.getMessage()]
        obs = coord._pending_observations[OBS_TYPE_HVAC_HEAT]
        assert obs["start_indoor_f"] == 66.5
        assert obs["peak_indoor_f"] == 66.5
        assert obs["start_outdoor_f"] == 41.0
        assert len(obs["active_samples"]) == 1
        assert obs["active_samples"][0]["indoor_temp_f"] == 66.5
        assert getattr(coord, "_rejection_log", {}) == {}


class TestAppendRejection:
    def test_abandon_observation_entry_shape_unchanged(self):
        coord = _make_outage_coord()
        coord._pending_observations[OBS_TYPE_HVAC_HEAT] = {
            "obs_type": OBS_TYPE_HVAC_HEAT,
            "start_time": (_FAKE_NOW - timedelta(minutes=5)).isoformat(),
            "active_samples": [
                {"timestamp": "t0", "indoor_temp_f": 65.0},
                {"timestamp": "t1", "indoor_temp_f": 66.0},
            ],
        }
        with patch(f"{_COORD_MOD}.dt_util", _make_dt_mock()):
            coord._abandon_observation(OBS_TYPE_HVAC_HEAT, "test")
        (event,) = coord._rejection_log[OBS_TYPE_HVAC_HEAT]
        assert set(event) == {
            "obs_type",
            "reason_code",
            "n_samples",
            "n_required",
            "r_squared",
            "r_squared_required",
            "delta_t_f",
            "delta_t_required",
            "elapsed_minutes",
            "sf_range",
            "indoor_direction",
            "timestamp",
        }
        assert event["reason_code"] == REJECT_ABANDONED
        assert event["n_samples"] == 2
        assert event["delta_t_f"] == 1.0
        assert event["indoor_direction"] == "rising"
        assert event["elapsed_minutes"] == 5
        assert coord.learning._state.rejection_log is coord._rejection_log
        assert coord.learning.save_state.call_count == 1

    def test_bucket_is_capped(self):
        coord = _make_outage_coord()
        cap = sys.modules[_COORD_MOD]._REJECTION_LOG_CAP
        for i in range(cap + 3):
            coord._append_rejection(OBS_TYPE_HVAC_HEAT, {"reason_code": "x", "i": i})
        bucket = coord._rejection_log[OBS_TYPE_HVAC_HEAT]
        assert len(bucket) == cap
        assert bucket[-1]["i"] == cap + 2


class TestLearningHealthCountsNewCode:
    def test_build_learning_health_counts_indoor_unavailable(self):
        coord = _make_outage_coord()
        coord.learning._state.thermal_observations = []
        _blank_indoor(coord)
        with patch(f"{_COORD_MOD}.dt_util", _make_dt_mock()):
            asyncio.run(coord._start_hvac_observation("heat"))
        coord._build_learning_health = types.MethodType(_get_coordinator_class()._build_learning_health, coord)
        health = coord._build_learning_health()
        heat = health[OBS_TYPE_HVAC_HEAT]
        assert heat["rejections"][REJECT_INDOOR_UNAVAILABLE] == 1
        assert heat["attempts"] == 1
        assert heat["last_rejection"]["reason_code"] == REJECT_INDOOR_UNAVAILABLE


class TestAiContextTreatsAsOperational:
    def test_indoor_unavailable_is_operational_not_quality_failure(self):
        health = _make_learning_health(hvac_heat_committed=2)
        health[OBS_TYPE_HVAC_HEAT]["rejections"][REJECT_INDOOR_UNAVAILABLE] = 3
        coord = _make_investigator_coord()
        coord._build_learning_health.return_value = health
        with patch(
            "custom_components.climate_advisor.ai_skills_context._fetch_github_issues",
            return_value="",
        ):
            ctx = asyncio.run(async_build_investigator_context(_make_investigator_hass(), coord))
        start = ctx.find(f"  {OBS_TYPE_HVAC_HEAT}: 2 committed")
        assert start != -1
        block = ctx[start : start + 400].split("\n  hvac_cool")[0]
        assert "operational interruptions: 3" in block
        assert "quality failures:" not in block
        assert "no quality failures" in block


class TestEventDrivenSample:
    @staticmethod
    def _coord_with_active_obs(*, indoor):
        coord = _make_thermostat_coord()
        # Real sample builder + real resolver inputs.
        cls = _get_coordinator_class()
        coord._get_current_sample = types.MethodType(cls._get_current_sample, coord)
        coord._ensure_pending_observations = types.MethodType(cls._ensure_pending_observations, coord)
        climate_state = MagicMock()
        climate_state.attributes = {"current_temperature": indoor}
        coord.hass.states.get = MagicMock(return_value=climate_state)
        coord.config["temp_unit"] = "fahrenheit"
        start = _FAKE_NOW - timedelta(minutes=10)
        coord._pending_observations = {
            OBS_TYPE_HVAC_HEAT: {
                "obs_type": OBS_TYPE_HVAC_HEAT,
                "status": "monitoring",
                "_phase": "active",
                "active_start": start.isoformat(),
                "active_samples": [],
                "peak_indoor_f": None,
            }
        }
        return coord

    @staticmethod
    def _fire(coord, caplog):
        old = _make_state("heat", hvac_action="heating")
        new = _make_state("heat", hvac_action="heating")
        with (
            patch(f"{_COORD_MOD}.dt_util", _make_dt_mock()),
            caplog.at_level(logging.DEBUG, logger=_COORD_MOD),
        ):
            asyncio.run(coord._async_thermostat_changed(_make_thermostat_event(old, new)))

    def test_indoor_none_skips_without_advancing_last_event_time(self, caplog):
        coord = self._coord_with_active_obs(indoor=None)
        prior = (_FAKE_NOW - timedelta(minutes=5)).isoformat()
        obs = coord._pending_observations[OBS_TYPE_HVAC_HEAT]
        obs["last_event_sample_time"] = prior
        self._fire(coord, caplog)
        assert obs["active_samples"] == []
        assert obs["last_event_sample_time"] == prior
        skips = [r for r in caplog.records if "Event-driven HVAC sample skipped" in r.getMessage()]
        assert len(skips) == 1
        assert skips[0].levelno == logging.DEBUG
        assert "indoor_unavailable" in skips[0].getMessage()
        assert not [
            r
            for r in caplog.records
            if r.levelno >= logging.INFO and "Event-driven HVAC sample skipped" in r.getMessage()
        ]

    def test_valid_indoor_appends_sample(self, caplog):
        coord = self._coord_with_active_obs(indoor=69.0)
        obs = coord._pending_observations[OBS_TYPE_HVAC_HEAT]
        self._fire(coord, caplog)
        assert len(obs["active_samples"]) == 1
        assert obs["active_samples"][0]["indoor_temp_f"] == 69.0
        assert obs["last_event_sample_time"] == _FAKE_NOW.isoformat()
        assert obs["peak_indoor_f"] == 69.0
