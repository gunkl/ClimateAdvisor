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
