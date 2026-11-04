"""Issue #1066: control-quality report (pure builder + shared diagnostic snapshot wiring)."""

from __future__ import annotations

import json
import re
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

from custom_components.climate_advisor.control_quality import (
    CONTROL_QUALITY_SCHEMA,
    build_control_quality,
    merge_chart_resolutions,
)
from custom_components.climate_advisor.diagnostic_snapshot import (
    build_diagnostic_snapshot,
    render_diagnostic_snapshot_text,
)

NOW = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)


def ts(minutes_ago: float, now: datetime = NOW) -> str:
    return (now - timedelta(minutes=minutes_ago)).isoformat()


def row(ago, hvac="idle", indoor=72.0, outdoor=70.0, lower=70.0, upper=74.0, pred=None):
    return {
        "ts": ts(ago),
        "hvac": hvac,
        "indoor": indoor,
        "outdoor": outdoor,
        "lower": lower,
        "upper": upper,
        "pred_indoor": pred,
    }


def change(ago, to_mode, indoor, floor=70.0, ceiling=74.0, from_mode=None, reason="selector"):
    return {
        "ts": ts(ago),
        "from_mode": from_mode,
        "to_mode": to_mode,
        "indoor_f": indoor,
        "floor_f": floor,
        "ceiling_f": ceiling,
        "margin_f": 2.0,
        "reason": reason,
    }


def build(changes=(), rows=(), history=None, now=NOW):
    return build_control_quality(now=now, mode_changes=list(changes), chart_entries=list(rows), indoor_history=history)


def w24(report):
    return report["windows"]["24h"]


class TestMisarmed:
    def test_single_episode(self):
        rows = [row(120, indoor=75), row(90, indoor=69), row(60, indoor=69), row(30, indoor=72)]
        m = w24(build([change(120, "cool", 75)], rows))["misarmed"]
        assert m["minutes"] == 60.0
        assert m["episodes"] == 1
        assert m["longest_minutes"] == 60.0
        assert m["percent_of_window"] == round(100 * 60 / 1440, 1)

    def test_back_to_back_episodes_are_separate(self):
        rows = [row(150, indoor=69), row(120, indoor=72), row(90, indoor=69), row(60, indoor=69), row(30, indoor=72)]
        m = w24(build([change(150, "cool", 75)], rows))["misarmed"]
        assert m["episodes"] == 2
        assert m["minutes"] == 90.0
        assert m["longest_minutes"] == 60.0

    def test_heat_armed_above_ceiling_counts(self):
        rows = [row(60, indoor=76), row(30, indoor=76)]
        m = w24(build([change(60, "heat", 66)], rows))["misarmed"]
        assert m["minutes"] == 60.0

    def test_zero_when_in_band(self):
        rows = [row(60, indoor=72), row(30, indoor=72)]
        m = w24(build([change(60, "cool", 75)], rows))["misarmed"]
        assert m["minutes"] == 0.0 and m["episodes"] == 0 and m["longest_minutes"] == 0.0

    def test_none_not_zero_without_ring_buffer(self):
        rows = [row(60, indoor=60), row(30, indoor=60)]
        m = w24(build([], rows))["misarmed"]
        assert m == {"minutes": None, "episodes": None, "longest_minutes": None, "percent_of_window": None}

    def test_mode_change_stops_misarm_at_next_change(self):
        rows = [row(90, indoor=69), row(0, indoor=69)]
        changes = [change(90, "cool", 75), change(60, "heat", 69)]
        m = w24(build(changes, rows))["misarmed"]
        assert m["minutes"] == 30.0  # cool armed 90..60 only; heat armed afterwards is correct

    def test_off_is_unarmed_and_accrues_nothing(self):
        rows = [row(90, indoor=69), row(60, indoor=69), row(30, indoor=69)]
        changes = [change(120, "cool", 75), change(95, "off", 69, floor=None, ceiling=None, reason="pause")]
        m = w24(build(changes, rows))["misarmed"]
        assert m["minutes"] == 0.0 and m["episodes"] == 0

    def test_unarmed_then_restore_scores_again(self):
        rows = [row(90, indoor=69), row(60, indoor=69), row(30, indoor=69)]
        changes = [change(95, "off", 69, floor=None, ceiling=None), change(65, "cool", 69, reason="restore")]
        m = w24(build(changes, rows))["misarmed"]
        assert m["minutes"] == 65.0  # record sample 65..60, then 60..30 and 30..now

    def test_none_bounds_on_armed_record_skip_scoring(self):
        rows = [row(60, indoor=60), row(30, indoor=60)]
        m = w24(build([change(90, "cool", 60, floor=None, ceiling=None)], rows))["misarmed"]
        assert m["minutes"] is None

    def test_none_and_unknown_modes_are_unarmed(self):
        rows = [row(60, indoor=60), row(30, indoor=60)]
        changes = [change(90, None, 60), change(80, "fan_only", 60)]
        assert w24(build(changes, rows))["misarmed"]["minutes"] == 0.0


class TestMisarmedAccuracy:
    """Review F8: dedupe, clipping and O(n) behaviour of the misarmed scan."""

    def test_duplicate_timestamps_count_once(self):
        # True misarmed time is 20 min (60..40 ago). Chart row, indoor-history sample and the
        # ring-buffer record all share the 60-minute timestamp (and 40 for the first two).
        rows = [row(60, indoor=69), row(40, indoor=72)]
        history = [(ts(60), 69.0), (ts(40), 72.0)]
        m = w24(build([change(60, "cool", 69)], rows, history=history))["misarmed"]
        assert m["minutes"] == 20.0
        assert m["episodes"] == 1

    def test_duplicate_timestamp_prefers_chart_row(self):
        rows = [row(60, indoor=72), row(40, indoor=72)]
        history = [(ts(60), 69.0)]  # same ts, would be misarmed; chart row (in band) wins
        m = w24(build([change(60, "cool", 69)], rows, history=history))["misarmed"]
        # the ring-buffer record (69.0) is also lower priority than the chart row
        assert m["minutes"] == 0.0

    def test_segment_straddling_window_start_is_clipped_not_dropped(self):
        # Window start is 1440 min ago. Sample at 1450 ago (misarmed) lasts until 1400 ago.
        rows = [row(1450, indoor=69), row(1400, indoor=72)]
        m = w24(build([change(1500, "cool", 75)], rows))["misarmed"]
        assert m["minutes"] == 40.0  # 1440..1400 only

    def test_window_start_clip_mirrored_heating(self):
        rows = [row(1450, indoor=76), row(1400, indoor=72)]
        m = w24(build([change(1500, "heat", 66)], rows))["misarmed"]
        assert m["minutes"] == 40.0

    def test_segment_ending_before_window_start_not_counted(self):
        rows = [row(1500, indoor=69), row(1480, indoor=72), row(10, indoor=72)]
        m = w24(build([change(1600, "cool", 75)], rows))["misarmed"]
        assert m["minutes"] == 0.0

    def test_five_thousand_samples_is_fast_and_linear(self):
        # Issue #1083: an absolute 0.1 s wall-clock limit flaked on loaded CI runners. What matters is that the
        # build stays linear. The sizes differ 4x, so linear code is ~4x slower and quadratic code ~16x; the 8x limit
        # sits a factor of two from each, so noise cannot fail linear code and a quadratic build cannot pass.
        # The previous 2x sizes with a 3.5x limit did both: suite load (other xdist workers and sessions) gave
        # 4-7x on linear code (15->102 ms, 20->78 ms, 26->104 ms), while an injected quadratic loop gave 2.6-3.3x.
        # Measured 2026-11-04: linear 3.2-6.0x under load, quadratic 15.6-17.4x. Runs are interleaved so a load burst
        # cannot land on one size only, and the best of 7 per size rides out spikes longer than a single run.
        def sample(n: int):
            rows = [row(m, indoor=69 if (m // 30) % 2 else 72) for m in range(n, 0, -1)]
            return [change(n, "cool", 69)], rows

        small, large = sample(1250), sample(5000)
        times: dict[int, list[float]] = {1250: [], 5000: []}
        for _ in range(7):
            for n, (changes, rows) in ((1250, small), (5000, large)):
                t0 = time.perf_counter()
                report = build(changes, rows, now=NOW)
                times[n].append(time.perf_counter() - t0)
                assert report["windows"]["7d"]["misarmed"]["minutes"] is not None
        t_small, t_large = min(times[1250]), min(times[5000])
        assert t_large < 3.0, f"build took {t_large * 1000:.0f} ms"
        assert t_large < max(8.0 * t_small, 0.05), (
            f"not linear: 1250 samples {t_small * 1000:.0f} ms -> 5000 samples {t_large * 1000:.0f} ms"
        )


class TestModeChanges:
    def test_latency_median_and_max(self):
        rows = [row(100, indoor=73), row(90, indoor=75), row(80, indoor=76), row(45, indoor=69)]
        changes = [change(60, "cool", 76, from_mode="heat"), change(30, "heat", 66, from_mode="cool")]
        mc = w24(build(changes, rows))["mode_changes"]
        assert mc["count"] == 2
        assert mc["per_day"] == 2.0
        assert mc["edge_to_armed_latency_s"]["max"] == 1800.0
        assert mc["edge_to_armed_latency_s"]["median"] == 1350.0

    def test_latency_ignores_off_and_none_bound_records(self):
        rows = [row(100, indoor=75), row(90, indoor=75)]
        changes = [change(60, "off", 75, floor=None, ceiling=None), change(50, "cool", 75, floor=None, ceiling=None)]
        mc = w24(build(changes, rows))["mode_changes"]
        assert mc["count"] == 2
        assert mc["edge_to_armed_latency_s"] is None

    def test_pre_arm_inside_band_is_unmeasurable(self):
        mc = w24(build([change(30, "cool", 73)], [row(60, indoor=72)]))["mode_changes"]
        assert mc["count"] == 1
        assert mc["edge_to_armed_latency_s"] is None

    def test_count_none_when_buffer_empty(self):
        mc = w24(build([], [row(30)]))["mode_changes"]
        assert mc["count"] is None and mc["per_day"] is None


class TestRuntime:
    def test_runs_medians_and_short_runs(self):
        rows = [
            row(100, "cooling"),
            row(90, "cooling"),
            row(80, "cooling"),
            row(70, "idle"),
            row(60, "idle"),
            row(50, "cooling"),
            row(45, "idle"),
            row(40, "idle"),
            row(35, "heating"),
            row(25, "heating"),
            row(15, "idle"),
        ]
        rt = w24(build([], rows))["runtime"]
        assert rt["cooling"] == {"minutes": 35.0, "runs": 2, "median_run_minutes": 17.5, "short_runs_lt_10m": 1}
        assert rt["heating"] == {"minutes": 20.0, "runs": 1, "median_run_minutes": 20.0, "short_runs_lt_10m": 0}

    def test_gap_is_capped_at_60_minutes(self):
        rt = w24(build([], [row(300, "cooling"), row(10, "idle")]))["runtime"]
        assert rt["cooling"]["minutes"] == 60.0
        assert rt["cooling"]["runs"] == 1

    def test_fan_only_is_not_running(self):
        rt = w24(build([], [row(60, "fan"), row(30, "idle")]))["runtime"]
        assert rt["cooling"]["minutes"] == 0.0 and rt["heating"]["minutes"] == 0.0

    def test_no_chart_data_gives_none(self):
        rt = w24(build([], []))["runtime"]
        assert rt["cooling"]["minutes"] is None and rt["heating"]["runs"] is None


class TestRunContext:
    def test_four_way_classification(self):
        rows = [
            row(200, "cooling", indoor=75, outdoor=80),
            row(190, "idle"),
            row(180, "cooling", indoor=75, outdoor=70),
            row(170, "idle"),
            row(160, "heating", indoor=68, outdoor=60),
            row(150, "idle"),
            row(140, "heating", indoor=68, outdoor=75),
            row(130, "idle"),
        ]
        ctx = w24(build([], rows))["run_context"]
        assert ctx == {
            "cooling_minutes_house_gaining_heat": 10.0,
            "cooling_minutes_passive_loss": 10.0,
            "heating_minutes_house_losing_heat": 10.0,
            "heating_minutes_passive_gain": 10.0,
        }

    def test_event_row_outdoor_is_forward_filled(self):
        rows = [row(60, "idle", outdoor=80), row(40, "cooling", indoor=75, outdoor=None), row(20, "idle", outdoor=80)]
        ctx = w24(build([], rows))["run_context"]
        assert ctx["cooling_minutes_house_gaining_heat"] == 20.0


def _cooling_defer_rows():
    return [
        row(90, "idle", indoor=75, outdoor=74, lower=70, upper=74),
        row(30, "cooling", indoor=75, outdoor=72, lower=70, upper=74),
        row(0, "idle", indoor=74, outdoor=71, lower=70, upper=74, pred=73.5),
    ]


def _mirror(rows, centre=144.0):
    """Reflect temperatures about ``centre`` and swap cooling/heating: a heating-side twin."""
    swap = {"cooling": "heating", "heating": "cooling"}
    out = []
    for r in rows:
        m = dict(r)
        m["hvac"] = swap.get(r["hvac"], r["hvac"])
        for key in ("indoor", "outdoor", "pred_indoor"):
            if r[key] is not None:
                m[key] = centre - r[key]
        m["lower"], m["upper"] = centre - r["upper"], centre - r["lower"]
        out.append(m)
    return out


class TestWouldHaveDeferred:
    def test_cooling_side_counts(self):
        d = w24(build([], _cooling_defer_rows()))["would_have_deferred"]
        assert d == {"cooling_minutes": 30.0, "heating_minutes": 0.0}

    def test_heating_mirror_gives_identical_result(self):
        cool = w24(build([], _cooling_defer_rows()))["would_have_deferred"]
        heat = w24(build([], _mirror(_cooling_defer_rows())))["would_have_deferred"]
        assert heat["heating_minutes"] == cool["cooling_minutes"] == 30.0
        assert heat["cooling_minutes"] == cool["heating_minutes"] == 0.0

    def test_not_deferred_when_outdoor_rising_both_sides(self):
        rows = _cooling_defer_rows()
        rows[0]["outdoor"] = 70  # outdoor 72 now vs 70 earlier = rising
        for r in (rows, _mirror(rows)):
            d = w24(build([], r))["would_have_deferred"]
            assert d["cooling_minutes"] == 0.0 and d["heating_minutes"] == 0.0

    def test_not_deferred_when_too_far_past_edge_both_sides(self):
        rows = _cooling_defer_rows()
        rows[1]["indoor"] = 76.5  # 2.5 F over the ceiling
        for r in (rows, _mirror(rows)):
            d = w24(build([], r))["would_have_deferred"]
            assert d["cooling_minutes"] == 0.0 and d["heating_minutes"] == 0.0

    def test_not_deferred_when_prediction_stays_outside_both_sides(self):
        rows = _cooling_defer_rows()
        rows[2]["pred_indoor"] = 75.5
        for r in (rows, _mirror(rows)):
            d = w24(build([], r))["would_have_deferred"]
            assert d["cooling_minutes"] == 0.0 and d["heating_minutes"] == 0.0


class TestWouldHaveDeferredStrictlyPast:
    def test_inside_band_not_deferrable_both_sides(self):
        rows = _cooling_defer_rows()
        rows[1]["indoor"] = 73.0  # inside the band (TOU pre-cool banking), 1 F under the ceiling
        for r in (rows, _mirror(rows)):
            d = w24(build([], r))["would_have_deferred"]
            assert d["cooling_minutes"] == 0.0 and d["heating_minutes"] == 0.0

    def test_exactly_on_edge_not_deferrable_both_sides(self):
        rows = _cooling_defer_rows()
        rows[1]["indoor"] = 74.0
        for r in (rows, _mirror(rows)):
            d = w24(build([], r))["would_have_deferred"]
            assert d["cooling_minutes"] == 0.0 and d["heating_minutes"] == 0.0

    def test_just_past_edge_is_deferrable_both_sides(self):
        rows = _cooling_defer_rows()
        rows[1]["indoor"] = 74.2
        cool = w24(build([], rows))["would_have_deferred"]
        heat = w24(build([], _mirror(rows)))["would_have_deferred"]
        assert cool["cooling_minutes"] == 30.0
        assert heat["heating_minutes"] == 30.0


class TestPredictionError:
    def _rows(self):
        return [
            row(120, indoor=70.0, pred=70.0),
            row(90, indoor=70.5, pred=71.0),
            row(60, indoor=72.5, pred=72.0),
            row(30, indoor=73.0, pred=None),
        ]

    def test_horizons_skip_missing_predictions(self):
        pe = w24(build([], self._rows()))["prediction_error"]
        assert pe["horizons"]["30m"] == {"mean_abs_f": 0.8, "mean_signed_f": -0.2, "n": 2}
        assert pe["horizons"]["60m"] == {"mean_abs_f": 0.5, "mean_signed_f": -0.5, "n": 1}
        assert pe["horizons"]["120m"] == {"mean_abs_f": None, "mean_signed_f": None, "n": 0}

    def test_archive_lead_error(self):
        lead = w24(build([], self._rows()))["prediction_error"]["archive_lead"]
        assert lead["n"] == 3
        assert lead["mean_signed_f"] == 0.0


class TestRobustness:
    def test_empty_inputs_give_structure_with_none_fields(self):
        r = build([], [])
        assert r["schema"] == CONTROL_QUALITY_SCHEMA == 3
        assert set(r["windows"]) == {"24h", "7d"}
        w = w24(r)
        assert w["samples"] == {"mode_changes": 0, "chart_entries": 0, "coverage_hours": None}
        assert w["misarmed"]["minutes"] is None
        assert w["would_have_deferred"] == {"cooling_minutes": None, "heating_minutes": None}
        assert w["prediction_error"]["horizons"]["30m"]["n"] == 0
        json.dumps(r)

    def test_malformed_rows_are_ignored(self):
        rows = [
            None,
            "x",
            {"ts": "garbage"},
            {"hvac": "cooling"},
            {"ts": ts(50), "hvac": "cooling", "indoor": True},
            {"ts": ts(40), "hvac": "cooling", "indoor": float("nan"), "outdoor": "hot"},
            row(30, "cooling"),
            row(20, "idle"),
        ]
        changes = [None, {"ts": "x"}, {"to_mode": "cool"}, change(15, "cool", 75)]
        r = build(changes, rows, history=["bad", (1, 2), ("nope", 70.0), (ts(25), 76.0)])
        assert w24(r)["mode_changes"]["count"] == 1
        assert w24(r)["runtime"]["cooling"]["runs"] >= 1
        json.dumps(r)

    def test_non_list_inputs_do_not_raise(self):
        r = build_control_quality(now=NOW, mode_changes=None, chart_entries="nope", indoor_history=5)  # type: ignore[arg-type]
        assert w24(r)["misarmed"]["minutes"] is None

    def test_window_slicing_24h_vs_7d(self):
        rows = [row(3000, "cooling"), row(2960, "idle"), row(120, "cooling"), row(100, "idle")]
        r = build([], rows)
        assert r["windows"]["24h"]["runtime"]["cooling"]["minutes"] == 20.0
        assert r["windows"]["7d"]["runtime"]["cooling"]["minutes"] == 60.0
        assert r["windows"]["24h"]["samples"]["chart_entries"] == 2
        assert r["windows"]["7d"]["samples"]["chart_entries"] == 4

    def test_naive_now_is_tolerated(self):
        r = build(now=NOW.replace(tzinfo=None))
        assert r["generated_at"].endswith("+00:00")

    def test_merge_chart_resolutions_prefers_raw_for_recent_three_days(self):
        old_hourly = {"ts": ts(60 * 24 * 5), "hvac": "idle"}
        recent_hourly = {"ts": ts(60 * 24 * 1), "hvac": "idle"}
        raw = {"ts": ts(60), "hvac": "cooling"}
        merged = merge_chart_resolutions([raw], [old_hourly, recent_hourly], NOW)
        assert merged == [old_hourly, raw]


_ENTITY_RE = re.compile(r"^[a-z_]+\.[a-z0-9_]+$")
_BAD_KEY_PARTS = ("entity", "friendly", "name", "token")


def _walk(node, path="root"):
    if isinstance(node, dict):
        for k, v in node.items():
            assert not any(p in str(k).lower() for p in _BAD_KEY_PARTS), f"identifying key {path}.{k}"
            yield from _walk(v, f"{path}.{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from _walk(v, f"{path}[{i}]")
    else:
        yield path, node


class TestRedaction:
    def test_no_entity_like_values_or_identifying_keys(self):
        rows = [row(m, "cooling" if m % 20 else "idle", pred=72.0) for m in range(300, 0, -10)]
        r = build([change(200, "cool", 75)], rows, history=[(ts(50), 71.0)])
        seen = 0
        for path, value in _walk(r):
            seen += 1
            if isinstance(value, str):
                assert not _ENTITY_RE.match(value), f"entity-like value at {path}: {value}"
        assert seen > 20
        json.dumps(r)


def _live_iso(minutes_ago):
    return (datetime.now(UTC) - timedelta(minutes=minutes_ago)).isoformat()


def _snapshot_coordinator(chart_log, changes):
    engine = SimpleNamespace(comfort_mode_changes=changes)
    return SimpleNamespace(
        config={"climate_entity": "climate.t", "temp_unit": "fahrenheit"},
        automation_engine=engine,
        _event_log=[],
        _chart_log=chart_log,
        _indoor_temp_history=[(_live_iso(20), 71.0)],
    )


def _hass():
    hass = MagicMock()
    hass.states.get.return_value = None
    return hass


class TestSnapshotIntegration:
    def test_section_added_through_real_builder(self):
        log = MagicMock()
        rows = [
            {"ts": _live_iso(90), "hvac": "cooling", "indoor": 75.0, "outdoor": 80.0, "lower": 70.0, "upper": 74.0},
            {"ts": _live_iso(60), "hvac": "idle", "indoor": 73.0, "outdoor": 79.0, "lower": 70.0, "upper": 74.0},
        ]
        log.get_entries.return_value = rows
        changes = [
            {
                "ts": _live_iso(95),
                "from_mode": "heat",
                "to_mode": "cool",
                "indoor_f": 75.0,
                "floor_f": 70.0,
                "ceiling_f": 74.0,
                "margin_f": 2.0,
                "reason": "selector",
            }
        ]
        snap = build_diagnostic_snapshot(_hass(), _snapshot_coordinator(log, changes))
        cq = snap["control_quality"]
        assert cq["schema"] == 3
        assert cq["windows"]["24h"]["mode_changes"]["count"] == 1
        assert cq["windows"]["24h"]["runtime"]["cooling"]["minutes"] == 30.0
        assert cq["windows"]["24h"]["samples"]["chart_entries"] == 2
        text = render_diagnostic_snapshot_text(snap)
        assert "control_quality (24h / 7d): misarmed_min=" in text
        json.dumps(snap["control_quality"])

    def test_raising_chart_log_yields_none_and_rest_of_snapshot_intact(self):
        log = MagicMock()
        log.get_entries.side_effect = RuntimeError("boom")
        snap = build_diagnostic_snapshot(_hass(), _snapshot_coordinator(log, []))
        assert snap["control_quality"] is None
        assert snap["version"]
        assert snap["config_summary"]["climate_entity"] == "climate.t"
        assert "control_quality (24h / 7d): (no data)" in render_diagnostic_snapshot_text(snap)

    def test_missing_chart_log_and_engine_ring_buffer_are_tolerated(self):
        coord = SimpleNamespace(config={}, automation_engine=None, _event_log=[])
        assert build_diagnostic_snapshot(_hass(), coord)["control_quality"] is None
        log = MagicMock()
        log.get_entries.return_value = []
        cq = build_diagnostic_snapshot(_hass(), _snapshot_coordinator(log, None))["control_quality"]
        assert cq["windows"]["24h"]["misarmed"]["minutes"] is None

    def test_include_control_quality_false_omits_key(self):
        log = MagicMock()
        log.get_entries.return_value = []
        coord = _snapshot_coordinator(log, [])
        snap = build_diagnostic_snapshot(_hass(), coord, include_control_quality=False)
        assert "control_quality" not in snap
        log.get_entries.assert_not_called()
        assert snap["version"]
        assert "control_quality" in build_diagnostic_snapshot(_hass(), coord)
        assert "control_quality (24h / 7d): (no data)" in render_diagnostic_snapshot_text(snap)
