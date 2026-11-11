"""Tests for thermal_replay.py --hvac mode (Phase C, Issue #130).

Tests cover:
  - detect_hvac_cycles(): cycle detection, window sizing, sample caps
  - run_cycle_replay(): the pooled cycle model through production cycle_model.fit_cycles (Issues #1512/#1516);
    the per-run run-edge replay and the duty-balance replay are retired
"""

from __future__ import annotations

import sys
import types
from datetime import UTC, datetime, timedelta

# ---------------------------------------------------------------------------
# Minimal sys.modules stubs so thermal_replay can be imported without SSH or
# any HA runtime.  thermal_replay.py is a pure-Python tool with no HA imports.
# ---------------------------------------------------------------------------


def _import_thermal_replay():
    import importlib

    return importlib.import_module("tools.thermal_replay")


# Make sure the tools package is importable
if "tools" not in sys.modules:
    tools_pkg = types.ModuleType("tools")
    tools_pkg.__path__ = ["tools"]
    sys.modules["tools"] = tools_pkg


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_BASE_TS = datetime(2026, 4, 16, 6, 0, 0, tzinfo=UTC)


def _ts(offset_minutes: int) -> str:
    """Return an ISO timestamp string offset from _BASE_TS."""
    return (_BASE_TS + timedelta(minutes=offset_minutes)).isoformat()


def _entry(
    offset_minutes: int,
    hvac: str = "idle",
    indoor: float = 68.0,
    outdoor: float | None = 50.0,
) -> dict:
    """Build a chart_log entry."""
    return {
        "ts": _ts(offset_minutes),
        "hvac": hvac,
        "indoor": indoor,
        "outdoor": outdoor,
        "fan": False,
        "windows_open": False,
    }


# ---------------------------------------------------------------------------
# Import the module under test
# ---------------------------------------------------------------------------

tr = _import_thermal_replay()


# ===========================================================================
# TestHvacCycleDetection
# ===========================================================================


class TestHvacCycleDetection:
    def test_detects_single_heat_cycle(self):
        """3 idle → 2 heating → 3 idle should produce exactly 1 cycle."""
        entries = (
            [_entry(i * 30, hvac="idle") for i in range(3)]
            + [_entry(90 + i * 30, hvac="heating", indoor=70.0) for i in range(2)]
            + [_entry(150 + i * 30, hvac="idle", indoor=69.0) for i in range(3)]
        )
        cycles = tr.detect_hvac_cycles(entries, min_total_samples=4)
        assert len(cycles) == 1
        c = cycles[0]
        assert c["mode"] == "heat"
        assert len(c["active_samples"]) == 2
        assert len(c["pre_samples"]) > 0
        assert len(c["post_samples"]) > 0

    def test_heat_and_cool_cycles_detected_separately(self):
        """heating block then cooling block → 2 cycles with correct modes."""
        entries = (
            [_entry(i * 30, hvac="idle") for i in range(2)]
            + [_entry(60 + i * 30, hvac="heating", indoor=72.0) for i in range(2)]
            + [_entry(120 + i * 30, hvac="idle", indoor=70.0) for i in range(2)]
            + [_entry(180 + i * 30, hvac="cooling", indoor=68.0) for i in range(2)]
            + [_entry(240 + i * 30, hvac="idle", indoor=69.0) for i in range(2)]
        )
        cycles = tr.detect_hvac_cycles(entries, min_total_samples=4)
        assert len(cycles) == 2
        assert cycles[0]["mode"] == "heat"
        assert cycles[1]["mode"] == "cool"

    def test_skips_cycle_with_too_few_total_samples(self):
        """1 pre + 1 active + 1 post = 3 total — below min_total_samples=4, skip."""
        entries = [
            _entry(0, hvac="idle"),
            _entry(30, hvac="heating", indoor=70.0),
            _entry(60, hvac="idle"),
        ]
        cycles = tr.detect_hvac_cycles(entries, min_total_samples=4)
        assert len(cycles) == 0

    def test_pre_samples_capped_at_60_min(self):
        """10 idle entries 30 min apart = 300 min before HVAC start.
        pre_samples must contain only entries within 60 min of start."""
        pre_entries = [_entry(i * 30, hvac="idle") for i in range(10)]  # 0..270 min
        active_entries = [_entry(300 + i * 30, hvac="heating", indoor=71.0) for i in range(2)]
        post_entries = [_entry(360 + i * 30, hvac="idle") for i in range(4)]
        entries = pre_entries + active_entries + post_entries
        cycles = tr.detect_hvac_cycles(entries, pre_window_minutes=60, min_total_samples=4)
        assert len(cycles) == 1
        # Only the entry at 270 min is within 60 min of start (300 min)
        for s in cycles[0]["pre_samples"]:
            ts = datetime.fromisoformat(s["ts"])
            active_start = datetime.fromisoformat(active_entries[0]["ts"])
            gap_min = (active_start - ts).total_seconds() / 60
            assert gap_min <= 60, f"pre sample is {gap_min:.1f} min before start — exceeds cap"

    def test_post_samples_capped_at_90_min(self):
        """10 idle entries 30 min apart after HVAC stop = 300 min.
        post_samples must contain only entries within 90 min of stop."""
        pre_entries = [_entry(i * 30, hvac="idle") for i in range(2)]
        active_entries = [_entry(60 + i * 30, hvac="heating", indoor=71.0) for i in range(2)]
        post_entries = [_entry(120 + i * 30, hvac="idle") for i in range(10)]  # 120..390 min
        entries = pre_entries + active_entries + post_entries
        cycles = tr.detect_hvac_cycles(entries, post_window_minutes=90, min_total_samples=4)
        assert len(cycles) == 1
        active_end = datetime.fromisoformat(active_entries[-1]["ts"])
        for s in cycles[0]["post_samples"]:
            ts = datetime.fromisoformat(s["ts"])
            gap_min = (ts - active_end).total_seconds() / 60
            assert gap_min <= 90, f"post sample is {gap_min:.1f} min after stop — exceeds cap"

    def test_outdoor_none_entries_included_in_window(self):
        """Entries with outdoor=None must NOT be excluded during cycle detection.
        Outdoor fallback is handled at OLS time, not during detection."""
        entries = (
            [_entry(i * 30, hvac="idle") for i in range(2)]
            + [_entry(60 + i * 30, hvac="heating", indoor=70.0, outdoor=None) for i in range(2)]
            + [_entry(120 + i * 30, hvac="idle", indoor=69.0, outdoor=None) for i in range(2)]
        )
        cycles = tr.detect_hvac_cycles(entries, min_total_samples=4)
        assert len(cycles) == 1
        # All active samples should be present even with outdoor=None
        assert len(cycles[0]["active_samples"]) == 2


# ===========================================================================
# TestCycleReplay
# ===========================================================================


class _FakeCycleModel:
    """Stands in for production cycle_model: records what the replay hands it (the DRY contract)."""

    def __init__(self):
        self.fit_calls = []
        self.prior_calls = []

    def prior_from_cache(self, cache, mode, now, *, cycles=None):
        self.prior_calls.append((cache, mode, now))
        self.prior_pools = [*getattr(self, "prior_pools", []), cycles]
        return {"mode": mode, "from": "cache"}

    def fit_cycles(self, cycles, mode, prior, *, now_ts):
        self.fit_calls.append((cycles, mode, prior, now_ts))
        return {
            "q": 7.5 if mode == "heat" else -8.0,
            "sd_q": 1.0,
            "grade": "medium",
            "coast_area_f": 0.4,
            "tau": 5.0,
            "ne": len(cycles),
            "n_cycles": len(cycles),
            "resolution_f": 0.1,
            "k_p_cycle": -0.05,
        }


class TestCycleReplay:
    """run_cycle_replay(): production cycle_model.fit_cycles over the stored cycles (Issues #1512/#1516)."""

    NOW = 1_800_000_000.0

    def _cycles(self):
        day = 86400
        return [
            {"id": "h1", "mode": "heat", "t_on": self.NOW - 2 * day},
            {"id": "h2", "mode": "heat", "t_on": self.NOW - 40 * day},
            {"id": "c1", "mode": "cool", "t_on": self.NOW - 1 * day},
        ]

    def test_replay_calls_the_production_functions_per_mode(self, monkeypatch):
        fake = _FakeCycleModel()
        monkeypatch.setattr(tr, "_production_cycle_model", lambda: fake)
        out = tr.run_cycle_replay(self._cycles(), {"k_active_heat": 6.0}, self.NOW)
        assert set(out) == {"heat", "cool"}
        by_mode = {m: (c, p) for c, m, p, _ in fake.fit_calls}
        assert [c["id"] for c in by_mode["heat"][0]] == ["h1", "h2"]
        assert [c["id"] for c in by_mode["cool"][0]] == ["c1"]
        # the prior is production's prior_from_cache over the learning DB's cache, not a second copy
        assert by_mode["heat"][1] == {"mode": "heat", "from": "cache"}
        assert fake.prior_calls[0][0] == {"k_active_heat": 6.0}
        # N5 (Issue #1512): the prior sees the same pool as the fit (a held fit is the prior only when its cycles are
        # not in it), as the live refit does
        assert [[c["id"] for c in pool] for pool in fake.prior_pools] == [["h1", "h2"], ["c1"]]

    def test_days_limits_the_cycles_used(self, monkeypatch):
        fake = _FakeCycleModel()
        monkeypatch.setattr(tr, "_production_cycle_model", lambda: fake)
        tr.run_cycle_replay(self._cycles(), None, self.NOW, days=30)
        heat = next(c for c, m, _, _ in fake.fit_calls if m == "heat")
        assert [c["id"] for c in heat] == ["h1"]  # the 40-day-old cycle is outside --days 30

    def test_report_lines_say_rate_grade_and_coast(self, monkeypatch):
        fake = _FakeCycleModel()
        monkeypatch.setattr(tr, "_production_cycle_model", lambda: fake)
        lines = tr.format_cycle_replay(tr.run_cycle_replay(self._cycles(), None, self.NOW))
        assert "[heat] q=+7.50" in lines[0] and "grade=medium" in lines[0] and "coast_area=+0.40" in lines[0]
        assert "(diagnostic only)" in lines[0]
        assert "[cool] q=-8.00" in lines[1]

    def test_old_run_edge_and_duty_balance_replays_are_gone(self):
        # Old expectation was wrong to exist: the per-run run-edge estimator and the duty-balance fit are retired
        # (the cycle model supersedes both; poc2 showed whole-degree short cycles cannot identify a rate per run).
        assert not hasattr(tr, "run_hvac_replay")
        assert not hasattr(tr, "run_duty_balance_replay")
        assert not hasattr(tr, "_production_fit")


# ===========================================================================
# TestSwingBackfill
# ===========================================================================


class TestSwingBackfill:
    """Swing backfill (Issue #1234): chart_log -> the thermostat's own hysteresis, via the shared
    pure rules in custom_components/climate_advisor/swing.py (no second copy in this tool)."""

    @staticmethod
    def _rows(mode, sp, on, off, prev_sp):
        run = "heating" if mode == "heat" else "cooling"
        return [
            {"ts": "2026-11-01T13:30:00+00:00", "hvac": "idle", "indoor": on + 0.3, "setpoint": prev_sp},
            {
                "ts": "2026-11-01T14:00:00+00:00",
                "hvac": run,
                "indoor": on,
                "indoor_primary": on,
                "setpoint": sp,
                "event": "hvac_action_change",
            },
            {
                "ts": "2026-11-01T14:08:00+00:00",
                "hvac": "idle",
                "indoor": off,
                "indoor_primary": off,
                "setpoint": sp,
                "event": "hvac_action_change",
            },
        ]

    def test_heat_start_point_below_setpoint(self):
        out = tr.compute_swing_samples_from_chart_log(self._rows("heat", 69.0, 67.64, 68.18, 69.0))
        assert [s["value_f"] for s in out["heat"]] == [1.36]
        assert out["cool"] == []

    def test_cool_start_point_above_setpoint(self):
        out = tr.compute_swing_samples_from_chart_log(self._rows("cool", 75.0, 76.4, 75.6, 75.0))
        assert [s["value_f"] for s in out["cool"]] == [1.4]

    def test_half_the_rise_is_no_longer_the_definition(self):
        """66 -> 70 under setpoint 69: the old |end-start|/2 gave 2.0; the start point is 3.0 below."""
        out = tr.compute_swing_samples_from_chart_log(self._rows("heat", 69.0, 66.0, 70.0, 69.0))
        assert [s["value_f"] for s in out["heat"]] == [3.0]

    def test_wrong_direction_and_non_positive_skipped(self):
        assert tr.compute_swing_samples_from_chart_log(self._rows("heat", 69.0, 67.0, 66.0, 69.0))["heat"] == []
        assert tr.compute_swing_samples_from_chart_log(self._rows("heat", 69.0, 69.5, 70.0, 69.0))["heat"] == []

    def test_setpoint_jump_or_unknown_previous_setpoint_skipped(self):
        assert tr.compute_swing_samples_from_chart_log(self._rows("heat", 69.0, 67.0, 68.5, 65.0))["heat"] == []
        assert tr.compute_swing_samples_from_chart_log(self._rows("heat", 69.0, 67.0, 68.5, None))["heat"] == []

    def test_jump_confirmed_only_by_a_row_written_after_the_change_is_skipped(self):
        """Live excludes a start 3-4 s after a setpoint/mode change; replay must not be looser.

        The 30-min poll row (written after apply_classification) and the ``override`` row both carry
        the NEW setpoint seconds before the start, so "same as the previous row" is no evidence.
        """
        poll_after_write = [
            {"ts": "2026-11-01T13:30:00+00:00", "hvac": "idle", "indoor": 67.6, "setpoint": 68.0},
            {"ts": "2026-11-01T14:00:00+00:00", "hvac": "idle", "indoor": 67.6, "setpoint": 70.0},
            {
                "ts": "2026-11-01T14:00:03+00:00",
                "hvac": "heating",
                "indoor": 67.6,
                "indoor_primary": 67.6,
                "setpoint": 70.0,
                "event": "hvac_action_change",
            },
            {
                "ts": "2026-11-01T14:15:00+00:00",
                "hvac": "idle",
                "indoor": 69.2,
                "indoor_primary": 69.2,
                "setpoint": 70.0,
                "event": "hvac_action_change",
            },
        ]
        assert tr.compute_swing_samples_from_chart_log(poll_after_write)["heat"] == []
        mode_override = [
            {"ts": "2026-11-01T13:00:00+00:00", "hvac": "off", "indoor": 66.0, "setpoint": None},
            {"ts": "2026-11-01T13:10:00+00:00", "hvac": "idle", "indoor": 66.0, "setpoint": 69.0, "event": "override"},
            {
                "ts": "2026-11-01T13:10:04+00:00",
                "hvac": "heating",
                "indoor": 66.0,
                "indoor_primary": 66.0,
                "setpoint": 69.0,
                "event": "hvac_action_change",
            },
            {
                "ts": "2026-11-01T13:25:00+00:00",
                "hvac": "idle",
                "indoor": 68.1,
                "indoor_primary": 68.1,
                "setpoint": 69.0,
                "event": "hvac_action_change",
            },
        ]
        assert tr.compute_swing_samples_from_chart_log(mode_override)["heat"] == []
        # Control: the same setpoint also confirmed by a row >= the window older IS a sample.
        confirmed = [{**poll_after_write[0], "setpoint": 70.0}] + poll_after_write[1:]
        assert [s["value_f"] for s in tr.compute_swing_samples_from_chart_log(confirmed)["heat"]] == [2.4]

    def test_poll_rows_are_not_edges(self):
        rows = [{**r, "event": None} for r in self._rows("heat", 69.0, 67.0, 68.5, 69.0)]
        assert tr.compute_swing_samples_from_chart_log(rows)["heat"] == []

    def test_whole_degree_log_reports_resolution_one(self):
        """Readings on the whole-F grid (3+ distinct) are inferred whole-degree from the readings."""
        rows = []
        for i, (on, off) in enumerate([(68.0, 68.0), (67.0, 68.0), (66.0, 69.0)]):
            base = f"2026-11-0{i + 1}"
            for r in self._rows("heat", 69.0, on, off, 69.0):
                rows.append({**r, "ts": r["ts"].replace("2026-11-01", base)})
        samples = tr.compute_swing_samples_from_chart_log(rows)["heat"]
        assert len(samples) == 3 and all(s["resolution_f"] == 1.0 for s in samples)

    def test_too_few_distinct_readings_is_not_called_whole_degree(self):
        """One repeated whole reading proves nothing about granularity; replay has no source to fall back on."""
        rows = []
        for i in range(3):
            base = f"2026-11-0{i + 1}"
            for r in self._rows("heat", 69.0, 68.0, 68.0, 69.0):
                rows.append({**r, "ts": r["ts"].replace("2026-11-01", base)})
        samples = tr.compute_swing_samples_from_chart_log(rows)["heat"]
        assert len(samples) == 3 and all(s["resolution_f"] == 0.0 for s in samples)

    def test_sleep_window_row_samples_the_primary_sensor_not_the_bedroom(self):
        """Issue #1234 D2: ``indoor`` is the sleep-window bedroom swap; the swing must use the hall (primary)."""
        rows = self._rows("heat", 69.0, 67.64, 68.18, 69.0)
        for r in rows[1:]:
            r["indoor"] = r["indoor"] - 2.0  # the bedroom, ~2 F colder than the hall
        samples = tr.compute_swing_samples_from_chart_log(rows)["heat"]
        assert [s["value_f"] for s in samples] == [1.36]  # setpoint - hall, not 3.36 from the bedroom

    def test_rows_without_indoor_primary_give_no_sample(self):
        """Historical chart_log rows (before v0.8.34) carry no primary reading: no backfill, live re-learns."""
        rows = self._rows("heat", 69.0, 67.64, 68.18, 69.0)
        for r in rows:
            r.pop("indoor_primary", None)
        assert tr.compute_swing_samples_from_chart_log(rows) == {"heat": [], "cool": []}

    def test_hvac_action_flap_gives_no_second_sample(self):
        """heating -> idle -> heating within seconds is one run, not a second drift to the trigger point."""
        rows = self._rows("heat", 69.0, 67.64, 68.18, 69.0)
        flap_on = {**rows[1], "ts": "2026-11-01T14:08:05+00:00", "indoor": 68.18, "indoor_primary": 68.18}
        flap_off = {**rows[2], "ts": "2026-11-01T14:12:00+00:00", "indoor": 68.5, "indoor_primary": 68.5}
        samples = tr.compute_swing_samples_from_chart_log([*rows, flap_on, flap_off])["heat"]
        assert [s["value_f"] for s in samples] == [1.36]

    def test_rebuild_does_not_recompute_swing_from_observations(self):
        """A legacy obs ``swing_f`` can never re-poison the cache."""
        obs = [
            {
                "timestamp": "2026-01-01T00:00:00+00:00",
                "date": "2026-01-01",
                "hvac_mode": "heat",
                "swing_f": 0.5,
                "confidence_grade": "low",
                "k_passive": None,
                "k_active": 5.0,
            }
        ]
        cache = tr.rebuild_model_cache(obs)
        assert cache["swing_heat_f"] is None
        assert cache["observation_count_swing_heat"] == 0

    def test_rebuild_carries_current_swing_and_resets_the_retired_one(self):
        current = {"swing_heat_f": 1.4, "observation_count_swing_heat": 7, "swing_definition_version": 2}
        assert tr.rebuild_model_cache([], existing_cache=current)["swing_heat_f"] == 1.4
        legacy = {"swing_heat_f": 0.59, "observation_count_swing_heat": 21}
        rebuilt = tr.rebuild_model_cache([], existing_cache=legacy)
        assert rebuilt["swing_heat_f"] is None and rebuilt["observation_count_swing_heat"] == 0

    def test_live_and_replayed_sample_for_the_same_cycle_do_not_double_count(self):
        cache = {}
        live = {"ts": "2026-11-01T14:00:00.123456+00:00", "value_f": 1.4, "resolution_f": 0.0, "off_point_f": 0.9}
        replay = {**live, "ts": "2026-11-01T14:00:00.123999+00:00"}
        tr._swing.apply_swing_samples(cache, "heat", [live])
        tr._swing.apply_swing_samples(cache, "heat", [replay])
        assert cache["observation_count_swing_heat"] == 1 and len(cache["swing_heat_samples"]) == 1


class TestWriteRetentionLikeProduction:
    """Issue #1523 mirror: the --write paths used to keep 90 days back from today and the newest 500, so writing the
    DB after a summer of vent fits dropped every closed-house fit (the leak rate then fell back to its pre-upgrade
    value). They now keep what production keeps: per-kind shares, nothing leaves with time."""

    def test_a_quiet_kind_survives_the_write(self):
        old_passive = [{"hvac_mode": "passive", "k_passive": -0.04, "date": "2025-01-01", "n": i} for i in range(20)]
        recent = [{"hvac_mode": "vent_window", "k_passive": -0.3, "date": "2026-11-01", "n": i} for i in range(10)]
        new = [{"hvac_mode": "passive", "k_passive": -0.04, "date": "2026-11-02", "source": "replay"}]
        existing = old_passive + recent
        kept = tr.retain_like_production(existing, existing + new)
        assert sum(1 for o in kept if o["hvac_mode"] == "passive") == 21

    def test_each_kind_keeps_its_share_or_what_it_held(self):
        learning = tr._production()
        share = learning.THERMAL_OBS_KIND_CAPS["vent_fan"]
        existing = [{"hvac_mode": "vent_fan", "date": "2026-01-01", "n": i} for i in range(share + 5)]
        new = [{"hvac_mode": "vent_fan", "date": "2026-02-01", "n": 100 + i} for i in range(3)]
        kept = tr.retain_like_production(existing, existing + new)
        assert len(kept) == share + 5  # held above its share: one in, one out
        assert [o["n"] for o in kept][-3:] == [100, 101, 102]
