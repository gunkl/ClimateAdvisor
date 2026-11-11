"""Tests for thermal_replay.py --hvac mode (Phase C, Issue #130).

Tests cover:
  - detect_hvac_cycles(): cycle detection, window sizing, sample caps
  - run_hvac_replay(): the run-edge estimator through production compute_hvac_run_rate (Issues #1512/#1516)
  - run_duty_balance_replay(): production hvac_rate_fit over chart_log rows
"""

from __future__ import annotations

import sys
import types
from datetime import UTC, datetime, timedelta

import pytest

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
# TestHvacRunEdgeReplay
# ===========================================================================


def _run_cycle(
    mode: str = "heat",
    start_f: float = 68.0,
    off_f: float = 69.5,
    minutes: float = 20.0,
    outdoor: float = 50.0,
    coast_f: float | None = None,
) -> dict:
    """A cycle with one active row at the on edge and post rows from the off edge.

    ``coast_f`` is where the room goes AFTER the off edge (furnace coast / AC rebound); the rate must not see it.
    """
    on = datetime(2026, 5, 3, 7, 0, 0, tzinfo=UTC)
    off = on + timedelta(minutes=minutes)
    post_indoor = [off_f] + ([coast_f, coast_f] if coast_f is not None else [off_f - 0.2, off_f - 0.4])
    return {
        "mode": mode,
        "pre_samples": [{"ts": (on - timedelta(minutes=30)).isoformat(), "indoor": start_f, "outdoor": outdoor}],
        "active_samples": [{"ts": on.isoformat(), "indoor": start_f, "outdoor": outdoor}],
        "post_samples": [
            {"ts": (off + timedelta(minutes=30 * i)).isoformat(), "indoor": t, "outdoor": outdoor}
            for i, t in enumerate(post_indoor)
        ],
        "start_ts": on.isoformat(),
    }


class TestHvacRunEdgeReplay:
    """run_hvac_replay(): the run-edge estimator via production compute_hvac_run_rate (Issues #1512, #1516)."""

    def test_heat_run_learns_rate_from_the_run_itself(self):
        obs, code = tr.run_hvac_replay(_run_cycle(), k_passive=-0.14)
        assert code is None and obs is not None
        # (69.5-68)/(1/3 h) = 4.5 F/hr gross; + 0.14 * (68.75-50) = 2.6 -> ~7.1 steady heating
        assert 6.5 < obs["k_active"] < 7.7
        assert obs["method"] == "run_edge"
        assert obs["source"] == "replay"
        assert obs["confidence_grade"] == "medium"  # 20 min >= 15

    def test_run_never_carries_a_closed_house_rate(self):
        obs, _ = tr.run_hvac_replay(_run_cycle(), k_passive=-0.14)
        # Old behaviour wrote the post-run fit's k_passive (or the open-window proxy) into the observation, so
        # a heating run could move the closed-house rate. A run now only USES k_passive, never feeds it.
        assert obs["k_passive"] is None
        assert obs["k_passive_used"] == -0.14

    def test_furnace_coast_after_off_edge_does_not_change_the_rate(self):
        # Old expectation was wrong: the old estimator took T_peak from active + the first two post rows, so a
        # furnace that kept warming the room after it stopped inflated the rate (#1512).
        plain, _ = tr.run_hvac_replay(_run_cycle(coast_f=None), k_passive=-0.14)
        coasting, _ = tr.run_hvac_replay(_run_cycle(coast_f=71.5), k_passive=-0.14)
        assert coasting["k_active"] == plain["k_active"]

    def test_cool_run_followed_by_rebound_still_learns(self):
        # Old expectation was wrong: the post-run "decay" check assumed the room cools after a run, which rejects
        # (or mis-fits) an AC run after which the room warms (#1516).
        cycle = _run_cycle(mode="cool", start_f=78.0, off_f=76.5, minutes=20.0, outdoor=88.0, coast_f=79.0)
        obs, code = tr.run_hvac_replay(cycle, k_passive=-0.14)
        assert code is None and obs is not None
        assert obs["hvac_mode"] == "cool"
        assert obs["k_active"] < 0

    def test_no_closed_house_rate_rejects_with_a_code(self):
        # Old behaviour borrowed the open-window rate (k_vent_window) here; owner decision: no stand-in.
        obs, code = tr.run_hvac_replay(_run_cycle(), k_passive=None)
        assert obs is None
        assert code == "no_k_passive"

    def test_short_run_rejects_with_a_code(self):
        obs, code = tr.run_hvac_replay(_run_cycle(minutes=3.0), k_passive=-0.14)
        assert obs is None
        assert code == "hvac_run_too_short"

    def test_signal_below_sensor_resolution_rejects(self):
        # 1.0 F steps: 2 x 1.0 = 2.0 F gate, a 1.5 F rise is not a real signal.
        obs, code = tr.run_hvac_replay(_run_cycle(), k_passive=-0.14, resolution_f=1.0)
        assert obs is None
        assert code == "hvac_signal_below_resolution"

    def test_flat_run_rejects(self):
        obs, code = tr.run_hvac_replay(_run_cycle(off_f=68.0), k_passive=-0.14)
        assert obs is None
        assert code == "hvac_signal_below_resolution"

    def test_replay_has_no_plateau_guard_or_proxy(self):
        # The drifted plateau guard and k_vent_window proxy branch are gone: nothing after the off edge gates a run.
        assert not hasattr(tr, "run_hvac_replay_ols")
        assert not hasattr(tr, "_THERMAL_HVAC_MIN_DECAY_F")
        flat_post = _run_cycle(coast_f=69.5)  # post rows never decay
        obs, code = tr.run_hvac_replay(flat_post, k_passive=-0.14)
        assert obs is not None and code is None


class TestDutyBalanceReplay:
    """run_duty_balance_replay(): production hvac_rate_fit over chart_log night rows (Issue #1512)."""

    def test_too_little_data_returns_none(self):
        rows = [{"ts": _ts(i * 30), "indoor": 68.0, "outdoor": 50.0, "hvac": "idle"} for i in range(5)]
        assert tr.run_duty_balance_replay(rows) is None

    def test_night_rows_with_heat_duty_fit_all_three_rates(self):
        # Synthesise 3 nights of 30-min rows from known rates and let production's fit recover them.
        k_p, k_h = -0.15, 6.0
        rows = []
        t = 68.0
        base = datetime(2026, 4, 1, 20, 0, tzinfo=UTC)
        for i in range(160):
            ts = base + timedelta(minutes=30 * i)
            # night only: 20:00-07:59 in each day (rows stay inside the non-solar hours)
            hvac = "heating" if i % 5 == 0 else "idle"
            rows.append(
                {"ts": ts.isoformat(), "indoor": round(t, 3), "outdoor": 45.0 + 3 * ((i % 8) / 8), "hvac": hvac}
            )
            rate = k_p * (t - rows[-1]["outdoor"]) + (k_h if hvac == "heating" else 0.0)
            t += rate * 0.5
        fit = tr.run_duty_balance_replay(rows, local_hour_of=lambda s: 0)  # treat every row as night
        assert fit is not None
        assert fit["k_passive"] == pytest.approx(-0.15, abs=0.03)
        assert fit["k_active_heat"] == pytest.approx(6.0, abs=0.6)
        assert fit["k_active_cool"] is None  # no cooling duty in the window: not identified


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
