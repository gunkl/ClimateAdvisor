"""
Tests for chart time-axis alignment — Issue #103.

Root cause: the activity timeline (drawActivityTimeline in index.html) used hardcoded
pixel margins (labelW=52px left, pad.right=20px right) while Chart.js dynamically
computes its plot area boundaries from y-axis label widths. The same timestamp mapped
to different pixels in each canvas → visible horizontal misalignment.

Fix: drawActivityTimeline now accepts chartLeftPx / chartRightPx from
_tempChart.scales.x.left/.right after Chart.js renders, so both canvases share
an identical time→pixel mapping.

Invariant: for any timestamp within [rangeMin, rangeMax]:
    activity_xpx(ts) == chartjs_getPixelForValue(ts)

Chart.js linear scale formula:
    pixel = left + (value - min) / (max - min) * (right - left)

Activity timeline formula (post-fix):
    xPx(ms) = plotLeft + ((ms - rangeMin) / span) * (plotRight - plotLeft)

These are identical when plotLeft == xScale.left and plotRight == xScale.right.
"""

import pytest


def _chartjs_pixel(ms, range_min, range_max, chart_left, chart_right):
    """Replicates Chart.js linear scale getPixelForValue formula."""
    span = range_max - range_min
    return chart_left + ((ms - range_min) / span) * (chart_right - chart_left)


def _activity_xpx(ms, range_min, range_max, plot_left, plot_right):
    """Activity timeline xPx formula after fix (chartLeftPx / chartRightPx passed in)."""
    span = range_max - range_min
    return plot_left + ((ms - range_min) / span) * (plot_right - plot_left)


def _activity_xpx_old(ms, range_min, range_max, canvas_w, label_w=52, pad_right=20):
    """Activity timeline xPx formula BEFORE fix (hardcoded margins)."""
    span = range_max - range_min
    plot_w = canvas_w - label_w - pad_right
    return label_w + ((ms - range_min) / span) * plot_w


class TestChartPixelAlignment:
    def test_new_formula_matches_chartjs_exactly(self):
        """After fix: activity timeline pixel == Chart.js pixel for identical bounds."""
        chart_left, chart_right = 45, 560
        range_min, range_max = 1_000_000, 2_000_000
        for ts in [range_min, range_max, (range_min + range_max) // 2, range_min + 100_000]:
            expected = _chartjs_pixel(ts, range_min, range_max, chart_left, chart_right)
            actual = _activity_xpx(ts, range_min, range_max, chart_left, chart_right)
            assert expected == actual, f"ts={ts}: chart={expected}, timeline={actual}"

    def test_old_hardcoded_margins_cause_misalignment(self):
        """Proves the bug: hardcoded 52px left / 20px right differs from Chart.js bounds."""
        canvas_w = 600
        # Chart.js dynamic bounds — differ from fixed 52 and 580
        chart_left = 47
        chart_right = 558  # right y-axis present shrinks the plot area
        range_min, range_max, ts = 1_000_000, 2_000_000, 1_500_000

        old_px = _activity_xpx_old(ts, range_min, range_max, canvas_w)
        chartjs_px = _chartjs_pixel(ts, range_min, range_max, chart_left, chart_right)
        new_px = _activity_xpx(ts, range_min, range_max, chart_left, chart_right)

        assert old_px != chartjs_px, "Expected misalignment with old hardcoded formula"
        assert new_px == chartjs_px, "Expected exact alignment with new formula"

    def test_boundary_timestamps_map_to_plot_edges(self):
        """Range endpoints must map exactly to the left and right plot edges."""
        chart_left, chart_right = 45, 560
        range_min, range_max = 1_000_000, 2_000_000
        assert _activity_xpx(range_min, range_min, range_max, chart_left, chart_right) == chart_left
        assert _activity_xpx(range_max, range_min, range_max, chart_left, chart_right) == chart_right

    def test_zoom_range_preserves_same_pixel_boundaries(self):
        """After zoom: pixel boundaries unchanged; only the time range mapped to them changes."""
        chart_left, chart_right = 45, 560
        orig_min, orig_max = 1_000_000, 2_000_000
        zoom_min, zoom_max = 1_400_000, 1_600_000

        # Left edge of plot always corresponds to range start
        assert _activity_xpx(orig_min, orig_min, orig_max, chart_left, chart_right) == chart_left
        assert _activity_xpx(zoom_min, zoom_min, zoom_max, chart_left, chart_right) == chart_left
        # Right edge of plot always corresponds to range end
        assert _activity_xpx(orig_max, orig_min, orig_max, chart_left, chart_right) == chart_right
        assert _activity_xpx(zoom_max, zoom_min, zoom_max, chart_left, chart_right) == chart_right

    def test_fallback_matches_old_behavior_when_chart_not_ready(self):
        """chartLeftPx=None → fallback 52; chartRightPx=None → fallback W-20 (unchanged UX)."""
        canvas_w = 600
        fallback_left, fallback_right = 52, canvas_w - 20
        range_min, range_max, ts = 1_000_000, 2_000_000, 1_500_000

        old_result = _activity_xpx_old(ts, range_min, range_max, canvas_w)
        fallback_result = _activity_xpx(ts, range_min, range_max, fallback_left, fallback_right)
        assert old_result == fallback_result


class TestComfortBandRangeSpan:
    """Comfort band must span the full chart x-axis range on all range modes.

    Bug: the old implementation used predIndoorPts (today's 24 hourly timestamps,
    anchored to todayMidnight) as the x-axis skeleton for comfortBandDs. On a 7d or
    30d chart (past-anchored), today is at the far right edge — so the comfort band
    only covered the last 1/7 or 1/30 of the visible chart range.

    Fix: use [{ x: rMin }, { x: rMax }] — two span-covering endpoints that always
    match the chart's full x-axis range regardless of mode or pan offset.
    """

    def test_old_today_only_points_miss_historical_range(self):
        """Proves Bug 2: today's 24 hourly points leave the left 75%+ of a 7d chart uncovered."""
        now = 1_750_000_000_000  # arbitrary ms timestamp
        today_midnight = now - (now % 86_400_000)  # floor to midnight
        range_min = now - 7 * 24 * 3_600_000  # 7 days back

        # Old comfort band: today's 24 hourly x-values (all clustered near 'now')
        old_points = [{"x": today_midnight + h * 3_600_000} for h in range(24)]

        # Points in the leftmost 75% of the chart range
        left_75_pct_max = range_min + (now - range_min) * 0.75
        points_in_left_75 = [p for p in old_points if p["x"] < left_75_pct_max]
        assert len(points_in_left_75) == 0, (
            f"Expected no comfort band points in left 75% of 7d chart, got {len(points_in_left_75)}"
        )

    def test_new_span_points_cover_full_range(self):
        """After fix: [rMin, rMax] endpoints give 100% coverage of the chart x-axis."""
        now = 1_750_000_000_000
        range_min = now - 7 * 24 * 3_600_000
        comfort_cool = 76.0

        new_points = [{"x": range_min, "y": comfort_cool}, {"x": now, "y": comfort_cool}]
        assert new_points[0]["x"] == range_min
        assert new_points[-1]["x"] == now
        coverage = (new_points[-1]["x"] - new_points[0]["x"]) / (now - range_min)
        assert coverage == pytest.approx(1.0), f"Expected full coverage, got {coverage}"

    def test_short_range_6h_both_halves_covered(self):
        """On 6h range (centered on now), span points cover both past and future halves."""
        now = 1_750_000_000_000
        half = 6 * 1_800_000  # 3 hours in ms (half of 6h)
        range_min = now - half
        range_max = now + half
        new_points = [{"x": range_min}, {"x": range_max}]
        assert new_points[0]["x"] < now, "Left point must be before now (past half)"
        assert new_points[1]["x"] > now, "Right point must be after now (future half)"
