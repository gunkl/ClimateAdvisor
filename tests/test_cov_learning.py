"""Coverage for learning.py edge paths (Silver test-coverage, #1644).

What the person at home gets from these paths: a corrupt or partial learning file never wipes the
house's learned behaviour beyond the broken field, a bad or degenerate measurement window is rejected
instead of teaching the thermal model a wrong rate, and the learning suggestions (advice only; they
never change a setting) are suppressed or worded correctly from the stored history.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from custom_components.climate_advisor import learning as learning_mod
from custom_components.climate_advisor.const import (
    REJECT_NO_K_PASSIVE,
    REJECT_SMALL_DELTA,
    REJECT_TOO_FEW_BLOCKS,
    REJECT_TOO_FEW_SAMPLES,
    THERMAL_K_PASSIVE_MAX,
    THERMAL_K_PASSIVE_MIN,
)
from custom_components.climate_advisor.learning import (
    DailyRecord,
    LearningEngine,
    _integrate_indoor_rk4,
    compute_k_active,
    compute_k_active_single_point,
    compute_k_env_solar,
    compute_k_passive,
    compute_k_passive_blocks,
    compute_k_passive_endpoint,
    compute_k_solar,
    migrate_solar_phase_keep,
    weighted_median,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _s(indoor: float, outdoor: float, minutes: float, **extra) -> dict:
    return {"indoor_temp_f": indoor, "outdoor_temp_f": outdoor, "elapsed_minutes": minutes, **extra}


def _entry(ts: str, indoor: float, outdoor: float) -> dict:
    return {"ts": ts, "indoor": indoor, "outdoor": outdoor}


def _engine(tmp_path: Path) -> LearningEngine:
    return LearningEngine(tmp_path)


def _write_db(tmp_path: Path, data) -> None:
    eng = _engine(tmp_path)
    eng._db_path.write_text(json.dumps(data))


# ---------------------------------------------------------------------------
# migrate_solar_phase_keep: unreadable stamps count as stale
# ---------------------------------------------------------------------------


class TestMigrateSolarPhaseKeep:
    def test_unreadable_primary_stamp_with_fresh_secondary_gives_secondary_the_offset(self):
        today = date.today()
        cache = {
            "solar_phase_offset_h": 2.0,
            "solar_phase_offset_last_obs_date": "not-a-date",
            "solar_phase_offset_ac_h": 1.0,
            "solar_phase_offset_ac_obs_count": 10,
            "solar_phase_offset_ac_last_obs_date": today.isoformat(),
        }
        assert migrate_solar_phase_keep(cache, today) is True
        assert cache["solar_phase_offset_keep"] == 0.0

    def test_unreadable_secondary_stamp_keeps_primary(self):
        today = date.today()
        cache = {
            "solar_phase_offset_h": 2.0,
            "solar_phase_offset_last_obs_date": "garbage",
            "solar_phase_offset_ac_h": 1.0,
            "solar_phase_offset_ac_obs_count": 10,
            "solar_phase_offset_ac_last_obs_date": None,
        }
        assert migrate_solar_phase_keep(cache, today) is True
        assert cache["solar_phase_offset_keep"] == 1.0


# ---------------------------------------------------------------------------
# Pure estimators: degenerate windows are rejected, never committed
# ---------------------------------------------------------------------------


class TestComputeKPassive:
    def test_single_sample_window_and_zero_dt_intervals_yield_too_few(self):
        pre = [_s(70, 50, 0)]
        post = [_s(70, 50, 10), _s(69, 50, 10), _s(68, 50, 10), _s(67, 50, 10), _s(66, 50, 10)]
        k, r2, code = compute_k_passive(post, pre_samples=pre)
        assert (k, r2, code) == (None, 0.0, REJECT_TOO_FEW_SAMPLES)


class TestComputeKPassiveBlocks:
    def test_empty_window(self):
        assert compute_k_passive_blocks([]) == (None, 0.0, REJECT_TOO_FEW_SAMPLES)

    def test_unreadable_first_stamp(self):
        assert compute_k_passive_blocks([{"indoor": 70, "outdoor": 50}]) == (None, 0.0, REJECT_TOO_FEW_SAMPLES)

    def test_naive_stamps_bad_rows_and_lonely_blocks_leave_too_few_blocks(self):
        t0 = datetime(2026, 1, 10, 0, 0)
        entries = [
            _entry(t0.isoformat(), 70, 50),
            _entry((t0 + timedelta(minutes=30)).isoformat(), 69.8, 50),
            {"ts": "bad", "indoor": 69, "outdoor": 50},  # skipped
            _entry((t0 + timedelta(minutes=70)).isoformat(), 69.5, 50),  # alone in its block
        ]
        assert compute_k_passive_blocks(entries) == (None, 0.0, REJECT_TOO_FEW_BLOCKS)

    def test_enough_naive_blocks_reach_the_ols(self):
        t0 = datetime(2026, 1, 10, 0, 0)
        entries = []
        temp = 72.0
        for i in range(16):  # 8 one-hour blocks of two entries each
            entries.append(_entry((t0 + timedelta(minutes=30 * i)).isoformat(), round(temp, 2), 40.0))
            temp = 40.0 + (temp - 40.0) * 0.98
        k, r2, code = compute_k_passive_blocks(entries)
        assert code is None
        assert k is not None and THERMAL_K_PASSIVE_MIN <= k <= THERMAL_K_PASSIVE_MAX


class TestIntegrateRk4:
    def test_zero_steps_is_one_step(self):
        args = (70.0, [0.0, 1.0], [50.0, 50.0], -0.1, 1.0)
        assert _integrate_indoor_rk4(*args, 0) == _integrate_indoor_rk4(*args, 1)
        assert _integrate_indoor_rk4(*args, 0) < 70.0


class TestComputeKPassiveEndpoint:
    def test_too_short(self):
        assert compute_k_passive_endpoint([_entry("2026-01-10T00:00:00", 70, 50)], -0.5, -0.001) is None

    def test_unreadable_stamp(self):
        w = [_entry("bad", 70, 50), _entry("2026-01-10T01:00:00", 69, 50)]
        assert compute_k_passive_endpoint(w, -0.5, -0.001) is None

    def test_unreadable_temperature(self):
        w = [_entry("2026-01-10T00:00:00", 70, "x"), _entry("2026-01-10T01:00:00", 69, 50)]
        assert compute_k_passive_endpoint(w, -0.5, -0.001) is None

    def test_zero_duration(self):
        w = [_entry("2026-01-10T00:00:00", 70, 50), _entry("2026-01-10T00:00:00", 69, 50)]
        assert compute_k_passive_endpoint(w, -0.5, -0.001) is None

    def test_exact_root_at_lower_bracket(self):
        # Indoor equals outdoor throughout: every k reproduces the end, the lower bracket is returned.
        w = [_entry("2026-01-10T00:00:00", 50, 50), _entry("2026-01-10T02:00:00", 50, 50)]
        assert compute_k_passive_endpoint(w, -0.5, -0.001) == -0.5

    def test_exact_root_at_upper_bracket(self):
        # A flat indoor with a 20 F gap is reproduced only by k = 0 (the upper bracket here).
        w = [_entry("2026-01-10T00:00:00", 70, 50), _entry("2026-01-10T02:00:00", 70, 50)]
        assert compute_k_passive_endpoint(w, -0.5, 0.0) == 0.0

    def test_exact_root_at_bisection_midpoint(self):
        w = [_entry("2026-01-10T00:00:00", 70, 50), _entry("2026-01-10T02:00:00", 70, 50)]
        assert compute_k_passive_endpoint(w, -1.0, 1.0) == 0.0

    def test_no_sign_change_rejects(self):
        # Indoor warms away from a colder outdoor: no decay rate reproduces it.
        w = [_entry("2026-01-10T00:00:00", 70, 50), _entry("2026-01-10T02:00:00", 75, 50)]
        assert compute_k_passive_endpoint(w, -0.5, -0.001) is None


class TestComputeKEnvSolar:
    def test_missing_values_and_zero_dt_leave_too_few_pairs(self):
        samples = [_s(70, 50, 0), _s(None, 50, 10), _s(69, 50, 10), _s(68, 50, 10), _s(67, 50, 20)]
        assert compute_k_env_solar(samples) == (None, None, None)

    def test_flat_solar_factor_falls_back(self):
        samples = [_s(70 - i, 50, 10 * i, solar_factor=0.5) for i in range(6)]
        assert compute_k_env_solar(samples) == (None, None, None)

    def test_singular_matrix_falls_back(self):
        # Indoor equals outdoor (zero delta) while solar varies: the normal equations are singular.
        samples = [_s(60 + i, 60 + i, 10 * i, solar_factor=0.1 * i) for i in range(6)]
        assert compute_k_env_solar(samples) == (None, None, None)


class TestComputeKActive:
    def test_fan_only_has_no_active_rate(self):
        assert compute_k_active([_s(70, 50, 0), _s(71, 50, 10)], -0.1, "fan_only") == (None, 0.0)

    def test_all_zero_dt_intervals(self):
        assert compute_k_active([_s(70, 50, 0), _s(71, 50, 0), _s(72, 50, 0)], -0.1, "heat") == (None, 0.0)

    def test_heat_rate_out_of_bounds(self):
        # 30 F in 10 minutes: physically implausible heating.
        assert compute_k_active([_s(60, 50, 0), _s(75, 50, 5), _s(90, 50, 10)], -0.1, "heat") == (None, 0.0)

    def test_cool_rate_out_of_bounds(self):
        # Warming while "cooling": not a cooling rate.
        assert compute_k_active([_s(70, 50, 0), _s(71, 50, 30), _s(72, 50, 60)], 0.0, "cool") == (None, 0.0)


class TestComputeKSolar:
    def test_too_few(self):
        assert compute_k_solar([_s(70, 50, 0)], -0.1) == (None, 0.0)

    def test_all_zero_dt(self):
        assert compute_k_solar([_s(70, 50, 0), _s(71, 50, 0)], -0.1) == (None, 0.0)

    def test_negative_gain_clamps_to_zero(self):
        # Indoor cools faster than conduction explains: no negative solar gain is credited.
        k, r2 = compute_k_solar([_s(70, 70, 0), _s(68, 70, 60), _s(66, 70, 120)], -0.1)
        assert k == 0.0
        assert r2 == 0.0  # constant rate: no variance to explain


class TestSinglePoint:
    @pytest.mark.parametrize(
        ("t_start", "t_peak", "hours", "mode"),
        [
            (70, 72, 0.0, "heat"),  # no duration
            (70, 70.2, 1.0, "heat"),  # signal below the floor
            (70, 69.8, 1.0, "cool"),  # signal below the floor
            (60, 90, 0.5, "heat"),  # 60 F/h heating: out of bounds
            (90, 60, 0.5, "cool"),  # -60 F/h cooling: out of bounds
        ],
    )
    def test_rejected(self, t_start, t_peak, hours, mode):
        assert compute_k_active_single_point(t_start, t_peak, hours, -0.05, 20.0, mode) is None

    def test_accepted_cool(self):
        k = compute_k_active_single_point(76, 74, 1.0, -0.05, 0.0, "cool")
        assert k == pytest.approx(-2.0)


class TestWeightedMedian:
    def test_no_positive_weight(self):
        assert weighted_median([(1.0, 0.0), (2.0, -1.0)]) is None

    def test_exact_split_is_midpoint(self):
        assert weighted_median([(1.0, 1.0), (3.0, 1.0)]) == 2.0


# ---------------------------------------------------------------------------
# load_state / save_state: a damaged file never takes more than the damaged field
# ---------------------------------------------------------------------------


class TestLoadState:
    def test_non_object_json_starts_fresh(self, tmp_path, caplog):
        _write_db(tmp_path, [1, 2, 3])
        eng = _engine(tmp_path)
        with caplog.at_level(logging.WARNING):
            eng.load_state()
        assert eng._state.records == []
        assert "not a JSON object" in caplog.text

    def test_damaged_fields_reset_individually(self, tmp_path, caplog):
        rec = {"date": date.today().isoformat(), "day_type": "mild", "trend_direction": "stable"}
        _write_db(
            tmp_path,
            {
                "records": [rec],
                "active_suggestions": "oops",
                "thermal_observations": [{"date": "2026-01-01"}, "junk"],
                "thermal_model_cache": ["not", "a", "dict"],
                "rejection_log": "nope",
                "last_shutdown_version": 5,
                "clean_shutdown": "yes",
                "user_initiated_restart": 1,
                "last_shutdown_kind": "explosion",
            },
        )
        eng = _engine(tmp_path)
        with caplog.at_level(logging.WARNING):
            eng.load_state()
        st = eng._state
        assert st.records == [rec]  # the good field survives
        assert st.active_suggestions == []
        assert st.thermal_observations == [{"date": "2026-01-01"}]
        # The list was discarded (a fresh cache is rebuilt by the ageing step), never kept as-is.
        assert not isinstance(st.thermal_model_cache, list)
        assert (st.thermal_model_cache or {}).get("k_passive") is None
        assert st.rejection_log == {}
        assert st.last_shutdown_version is None
        assert st.clean_shutdown is False
        assert st.user_initiated_restart is False
        assert st.last_shutdown_kind is None
        assert "'active_suggestions' is not a list" in caplog.text
        assert "'thermal_model_cache' has unexpected type" in caplog.text

    def test_rejection_log_entries_validated_and_capped(self, tmp_path):
        _write_db(
            tmp_path,
            {"rejection_log": {"passive_decay": [{"i": i} for i in range(150)], "solar_gain": "bad"}},
        )
        eng = _engine(tmp_path)
        eng.load_state()
        log = eng._state.rejection_log
        assert log["solar_gain"] == []
        assert len(log["passive_decay"]) == 100
        assert log["passive_decay"][0] == {"i": 50}  # the newest 100 are kept


class TestSaveState:
    def test_unserializable_state_logs_and_writes_nothing(self, tmp_path, caplog):
        eng = _engine(tmp_path)
        eng._state.records.append({"bad": object()})
        with caplog.at_level(logging.ERROR):
            eng.save_state()
        assert "Failed to serialize learning state" in caplog.text
        assert not eng._db_path.exists()

    def test_replace_failure_logs_and_removes_temp_file(self, tmp_path, caplog):
        eng = _engine(tmp_path)
        with (
            patch.object(learning_mod.os, "replace", side_effect=OSError("disk full")),
            caplog.at_level(logging.ERROR),
        ):
            eng.save_state()
        assert "Failed to save learning state" in caplog.text
        assert not eng._db_path.exists()
        assert list(tmp_path.glob("*.tmp")) == []


# ---------------------------------------------------------------------------
# Recorders: invalid input is a no-op, caps hold
# ---------------------------------------------------------------------------


class TestRecorders:
    def test_record_day_hard_cap(self, tmp_path):
        eng = _engine(tmp_path)
        today = date.today().isoformat()
        with patch.object(learning_mod, "MAX_DAILY_RECORDS", 3):
            for i in range(5):
                eng.record_day(DailyRecord(date=today, day_type=f"d{i}", trend_direction="stable"))
        assert [r["day_type"] for r in eng._state.records] == ["d2", "d3", "d4"]

    def test_record_thermal_observation_rejects_non_observation(self, tmp_path):
        eng = _engine(tmp_path)
        assert eng.record_thermal_observation({"k_passive": -0.1}) is False
        assert eng.record_thermal_observation("nope") is False  # type: ignore[arg-type]
        assert eng._state.thermal_observations == []

    def test_apply_cycle_fit_rejects_bad_mode(self, tmp_path):
        eng = _engine(tmp_path)
        assert eng.apply_cycle_fit("fan_only", {"q": 2.0}, True) is False
        assert eng.apply_cycle_fit("heat", "not a dict", True) is False  # type: ignore[arg-type]
        assert eng._state.thermal_model_cache is None

    @pytest.mark.parametrize("method", ["record_swing_sample", "record_coast_sample"])
    def test_swing_and_coast_reject_bad_input(self, tmp_path, method):
        eng = _engine(tmp_path)
        getattr(eng, method)("fan_only", {"ts": "2026-01-01T00:00:00"})
        getattr(eng, method)("heat", {"no_ts": 1})
        assert eng._state.thermal_model_cache is None

    def test_swing_audit_rejects_bad_input(self, tmp_path):
        eng = _engine(tmp_path)
        eng.record_swing_audit("fan_only", {})
        eng.record_swing_audit("heat", "x")  # type: ignore[arg-type]
        assert eng._state.thermal_model_cache is None


class TestHeldRate:
    def test_low_keep_or_bad_q_gives_no_held_rate(self):
        cache = {"cycle_fit_heat": {"held": {"applied": True, "q": 2.0, "keep": 0.0}}}
        assert LearningEngine._held_rate(cache, "heat", 1.0) is None
        cache = {"cycle_fit_heat": {"held": {"applied": True, "q": True, "keep": 1.0}}}
        assert LearningEngine._held_rate(cache, "heat", 1.0) is None

    def test_no_aged_or_prior_value_returns_held_q(self):
        cache = {"cycle_fit_cool": {"held": {"applied": True, "q": -2.5, "keep": 0.5}}}
        assert LearningEngine._held_rate(cache, "cool", None) == -2.5

    def test_prior_value_blends_with_held_q(self):
        cache = {
            "cycle_fit_heat": {"held": {"applied": True, "q": 4.0, "keep": 0.5}},
            "rate_prior": {"k_active_heat": {"value": 2.0}},
        }
        assert LearningEngine._held_rate(cache, "heat", None) == 3.0


class TestThermalModelCache:
    def test_legacy_k_vent_key_logged_once(self, tmp_path, caplog):
        eng = _engine(tmp_path)
        eng._state.thermal_model_cache = {"k_vent": -0.2}
        obs = {"date": "2026-01-01", "hvac_mode": "passive", "k_passive": -0.1, "confidence_grade": "low"}
        with caplog.at_level(logging.INFO):
            eng._update_thermal_model_cache(dict(obs))
            eng._update_thermal_model_cache(dict(obs))
        assert caplog.text.count("legacy k_vent") == 1

    def test_solar_observations_blend_k_solar(self, tmp_path):
        eng = _engine(tmp_path)
        eng._state.thermal_model_cache = {}
        eng._update_thermal_model_cache(
            {"date": "d1", "hvac_mode": "solar", "k_solar": 2.0, "confidence_grade": "high"}
        )
        assert eng._state.thermal_model_cache["k_solar"] == 2.0
        eng._update_thermal_model_cache(
            {"date": "d2", "hvac_mode": "solar", "k_solar": 4.0, "confidence_grade": "high"}
        )
        cache = eng._state.thermal_model_cache
        assert cache["k_solar"] == pytest.approx(0.7 * 2.0 + 0.3 * 4.0)
        assert cache["observation_count_solar"] == 2
        assert cache["last_observation_date"] == "d2"

    def test_vent_observation_without_k_still_counts(self, tmp_path):
        eng = _engine(tmp_path)
        eng._state.thermal_model_cache = {}
        eng._update_thermal_model_cache({"date": "d1", "hvac_mode": "vent_fan", "k_passive": None})
        cache = eng._state.thermal_model_cache
        assert cache["observation_count_vent_fan"] == 1
        assert cache.get("k_vent_fan") is None


class TestDropDuplicateChartLogFits:
    def _fit(self, **kw) -> dict:
        base = {
            "source": "endpoint",
            "hvac_mode": "passive",
            "window_start": "2026-01-01T00:00:00",
            "window_end": "2026-01-01T06:00:00",
            "k_passive": -0.1,
            "confidence_grade": "low",
        }
        base.update(kw)
        return base

    def test_repeats_removed_without_a_cache(self, tmp_path):
        eng = _engine(tmp_path)
        eng._state.thermal_observations = [self._fit(), self._fit()]
        eng._state.thermal_model_cache = None
        eng._drop_duplicate_chart_log_fits()
        assert len(eng._state.thermal_observations) == 1

    def test_cache_k_passive_kept_when_the_list_does_not_explain_it(self, tmp_path, caplog):
        eng = _engine(tmp_path)
        eng._state.thermal_observations = [self._fit(), self._fit()]
        eng._state.thermal_model_cache = {"k_passive": -0.3, "observation_count_passive": "x"}
        with caplog.at_level(logging.INFO):
            eng._drop_duplicate_chart_log_fits()
        assert eng._state.thermal_model_cache["k_passive"] == -0.3
        assert eng._state.thermal_model_cache["observation_count_passive"] == "x"
        assert "k_passive kept" in caplog.text


# ---------------------------------------------------------------------------
# Read-side helpers
# ---------------------------------------------------------------------------


def _bias_records(n: int) -> list[dict]:
    return [
        {"forecast_high_f": 80, "observed_high_f": 83, "forecast_low_f": 60, "observed_low_f": 60} for _ in range(n)
    ]


class TestWeatherBias:
    @pytest.mark.parametrize(("n", "conf"), [(10, "low"), (20, "medium"), (30, "high")])
    def test_confidence_grows_with_count(self, tmp_path, n, conf):
        eng = _engine(tmp_path)
        eng._state.records = _bias_records(n)
        bias = eng.get_weather_bias()
        assert bias["confidence"] == conf
        assert bias["high_bias"] == 3.0
        assert bias["observation_count"] == n


class TestRecentRecords:
    def test_returns_the_tail(self, tmp_path):
        eng = _engine(tmp_path)
        eng._state.records = [{"i": i} for i in range(5)]
        assert eng.get_recent_records(2) == [{"i": 3}, {"i": 4}]

    def test_non_list_records(self, tmp_path):
        eng = _engine(tmp_path)
        eng._state.records = "broken"  # type: ignore[assignment]
        assert eng.get_recent_records(2) == []

    def test_unreadable_state(self, tmp_path):
        class _Boom:
            @property
            def records(self):
                raise RuntimeError("boom")

        eng = _engine(tmp_path)
        eng._state = _Boom()  # type: ignore[assignment]
        assert eng.get_recent_records(2) == []


# ---------------------------------------------------------------------------
# Suggestions (advice only): wording and suppression from stored history
# ---------------------------------------------------------------------------


def _days(n: int, **kw) -> list[dict]:
    base = {"day_type": "cold", "manual_overrides": 0, "hvac_runtime_minutes": 0}
    base.update(kw)
    return [dict(base) for _ in range(n)]


class TestSuggestionMsgs:
    def test_overrides_with_unreadable_times_get_the_plain_detail(self, tmp_path):
        eng = _engine(tmp_path)
        eng._state.records = _days(
            14, manual_overrides=1, override_details=[{"direction": "down", "magnitude": 2, "time": "x"}]
        )
        msgs = eng.generate_suggestion_msgs()
        keys = eng.get_last_suggestion_keys()
        assert keys == ["frequent_overrides"]
        detail = msgs[0].params["detail"]
        assert detail.key == "learning.override_detail"
        assert detail.params["direction"] == "down"

    def test_history_suppression_skips_bad_stamps_and_stops_at_the_cutoff(self, tmp_path):
        eng = _engine(tmp_path)
        eng._state.records = _days(14, manual_overrides=1)
        now = datetime.now()
        eng._state.settings_history = [
            # Older than 30 days: never reached (iteration stops at the cutoff).
            {
                "type": "feedback",
                "verdict": "incorrect",
                "suggestion": "short_departures",
                "timestamp": (now - timedelta(days=40)).isoformat(),
            },
            # Legacy accept (no type) inside 30 days: suppresses frequent_overrides.
            {"suggestion": "frequent_overrides", "timestamp": (now - timedelta(days=2)).isoformat()},
            {"type": "feedback", "verdict": "incorrect", "suggestion": "x", "timestamp": "not-a-time"},
        ]
        assert eng.generate_suggestion_msgs() == []
        assert eng.get_last_suggestion_keys() == []

    def test_dismissed_override_pattern_and_short_runtime_add_nothing(self, tmp_path):
        eng = _engine(tmp_path)
        eng._state.records = _days(14, day_type="mild", manual_overrides=1, hvac_runtime_minutes=30)
        eng._state.dismissed_suggestions = ["frequent_overrides"]
        assert eng.generate_suggestion_msgs() == []

    def test_thermal_model_ready_once(self, tmp_path):
        eng = _engine(tmp_path)
        eng._state.records = _days(14)
        with patch.object(eng, "get_thermal_model", return_value={"confidence": "low"}):
            eng.generate_suggestion_msgs()
            assert eng.get_last_suggestion_keys() == ["thermal_model_ready"]
            eng._state.active_suggestions = [{"key": "thermal_model_ready"}]
            eng.generate_suggestion_msgs()
            assert eng.get_last_suggestion_keys() == []


class TestCommitSolarGain:
    def test_too_few_samples(self, tmp_path):
        eng = _engine(tmp_path)
        assert eng._commit_event_from_dict({"samples": [_s(70, 50, 0)]}, None, "solar_gain") == (
            None,
            REJECT_TOO_FEW_SAMPLES,
            None,
        )

    def test_no_elapsed_time(self, tmp_path):
        eng = _engine(tmp_path)
        assert eng._commit_event_from_dict({"samples": [_s(70, 50, 0), _s(71, 50, 0)]}, None, "solar_gain") == (
            None,
            REJECT_SMALL_DELTA,
            None,
        )

    def test_no_envelope_model_yet(self, tmp_path):
        eng = _engine(tmp_path)
        samples = [_s(70, 50, 0), _s(71, 50, 30)]
        assert eng._commit_event_from_dict({"samples": samples}, None, "solar_gain") == (
            None,
            REJECT_NO_K_PASSIVE,
            None,
        )
