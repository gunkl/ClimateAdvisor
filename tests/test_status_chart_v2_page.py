"""Issue #1230 R2: static checks for the Status chart v2 in ``frontend/index.html``.

There is no JS runtime in the unit tier, so (like ``test_prediction_accuracy_page.py``) these read the page source and
assert what matters: every control exists with its aria wiring, every toggle and group is in the key, ranges / scroll /
zoom / pin / table / remembered choices / units / the v4 gate are wired, and only bundled libraries load. The behaviour
is exercised in a real browser (Playwright) by the R2 check recorded in the PR.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_FE = ROOT / "custom_components" / "climate_advisor" / "frontend"
# R6fe: the chart lives in the shared module chart_v2.js (and chart_v2.css); the classic page loads both. The checks
# below read the classic page plus those two files as one source, as they did when the chart was inline.
INDEX = "\n".join((_FE / n).read_text(encoding="utf-8") for n in ("index.html", "chart_v2.js", "chart_v2.css"))


def _func(name: str) -> str:
    # the shared module's definition wins over a page-level wrapper of the same name (classic's loadChart)
    module = (_FE / "chart_v2.js").read_text(encoding="utf-8")
    m = re.search(rf"  (?:async )?function {name}\(.*?\n  \}}\n", module, re.DOTALL) or re.search(
        rf"  (?:async )?function {name}\(.*?\n  \}}\n", INDEX, re.DOTALL
    )
    assert m, f"{name} not found"
    return m.group(0)


def test_two_charts_one_strip_canvas_and_a_cursor():
    for cid in ("temp-chart", "out-chart", "activity-chart", "chart-cursor", "chart-hover-panel", "chart-key"):
        assert f'id="{cid}"' in INDEX, cid
    assert "chart-container-out" in INDEX
    # the plot areas of the two charts line up: fixed axis widths and a fixed right gutter
    assert "const OUT_PX = 44" in INDEX and "const RIGHT_PX = 40" in INDEX
    assert "sc.width = OUT_PX" in INDEX and "sc.width = RIGHT_PX" in INDEX
    # Issue #1414: both charts carry the same RIGHT_PX-wide right % axis (its labels blank while no humidity line is
    # drawn on that chart), so the outdoor chart no longer pads its right side
    draw = _func("drawChart")
    assert draw.count("yRight: rightAxis(") == 2 and "padRight: RIGHT_PX" not in draw


def test_time_controls_past_ahead_scroll_now():
    for r in ("6h", "12h", "24h", "3d", "7d", "30d", "1y"):
        assert f'data-range="{r}"' in INDEX and f"setChartRange('{r}', this)" in INDEX
    for a in ("24h", "48h", "5d", "7d"):
        assert f'data-ahead="{a}"' in INDEX and f"setChartAhead('{a}')" in INDEX
    # the "now" view always shows at least a day ahead, on both pages (no 12h Ahead button; a stored '12h' migrates)
    new_page = (_FE / "new_dashboard.html").read_text(encoding="utf-8")
    assert 'data-ahead="12h"' not in INDEX and 'data-ahead="12h"' not in new_page
    assert "const CHART_AHEADS = ['24h', '48h', '5d', '7d']" in INDEX and "const MIN_AHEAD_H = 24" in INDEX
    assert 'onclick="shiftChartWindow(-1)"' in INDEX and 'onclick="shiftChartWindow(1)"' in INDEX
    assert 'onclick="chartGoNow()"' in INDEX
    assert "const CHART_RANGES = ['6h', '12h', '24h', '3d', '7d', '30d', '1y']" in INDEX
    assert "const CHART_LEADS = ['1h', '4h', '12h', '24h', '48h', '72h', '120h']" in INDEX


def test_scroll_is_half_a_view_and_loads_older_data_on_demand():
    assert "(w.max - w.min) / 2" in _func("_scrollStep")
    shift = _func("shiftChartWindow")
    assert "loadChart(vpEnd)" in shift and "loadChart();" in shift
    load = _func("loadChart")
    assert "before_ts=" in load and "_coveringRange(" in load  # a scrolled fetch covers past and ahead


def test_window_is_past_plus_ahead_and_zoom_is_remembered_across_redraws():
    base = _func("_baseWindow")
    assert "_hoursOf(_chartRange)" in base and "_hoursOf(_chartAhead)" in base
    assert "return _zoomWin || _baseWindow()" in _func("_viewWindow")


def test_zoom_drag_and_pinch_apply_to_both_charts_with_reset():
    assert "drag: { enabled: true" in INDEX and "pinch: { enabled: true }" in INDEX
    z = _func("_onZoomed")
    assert "_zoomWin = {" in z and "_applyWindow()" in z
    ap = _func("_applyWindow")
    assert "[_tempChart, _outChart]" in ap and "drawStrips()" in ap
    assert "resetZoom" in _func("resetChartZoom") and 'id="chart-zoom-reset"' in INDEX


def test_every_toggle_group_is_in_the_key():
    key = _func("renderKey")
    for group in ("Indoor", "Predicted", "Outdoor", "Overlays", "Activity"):
        assert f"_group('{group}'" in key, group
    for sid in ("ov:band", "ov:target", "ov:plan", "strip:events"):
        assert f"'{sid}'" in key, sid
    # Issue #1414: humidity / feels-like are ordinary indoor and outdoor series, not overlays; the old overlay
    # choices move to the indoor lines (and are re-saved) when the prefs are read
    for sid in ("ov:humidity", "ov:feelslike"):
        assert f"'{sid}'" not in key, sid
    assert "HIDDEN_OVERLAYS" not in INDEX
    assert "const OLD_OVERLAY_IDS = { 'ov:humidity': 'ind:humidity', 'ov:feelslike': 'ind:feelslike' }" in INDEX
    assert "OLD_OVERLAY_IDS" in _func("_readPrefs") and "prefs.migrated = true" in _func("_readPrefs")
    # Issue #1459: a strip the home cannot have (capabilities) has no chip
    assert "STRIP_ROWS.filter(r => !_stripLock(r.id)).map(r => ovChip('strip:' + r.id" in key
    for row in ("heating", "cooling", "fan", "natvent", "windows", "windowsplan"):
        assert f"id: '{row}'" in INDEX, row
    ser = _func("_buildSeries")
    for sid in (
        "'ind:' + m.id",
        "'ind:_ca'",
        "'v3'",
        "'v4:' + m.id",
        "'base:' + m.id",
        "'out:sensor'",
        "'out:service'",
        "'out:corrected'",
        "'ind:humidity'",
        "'ind:feelslike'",
        "'out:humidity'",
        "'out:feelslike'",
    ):
        assert sid in ser, sid
    assert "axis: 'yRight', unit: '%'" in ser  # humidity: right % axis
    assert "yAxisID: s.axis || 'y'" in _func("_dsFor")
    # chips are real buttons with aria-pressed; the key is one delegated handler
    chip = _func("_chip")
    assert '<button type="button"' in chip and "aria-pressed" in chip
    assert "data-series" in INDEX and "closest('[data-series]')" in INDEX


def test_defaults_baseline_overlays_and_ca_reading_start_off():
    d = _func("_defaultOn")
    assert "id.startsWith('base:')) return false" in d
    assert "HUM_FEELS_IDS.includes(id)) return false" in d  # humidity / feels-like start off (Issue #1414)
    assert "const HUM_FEELS_IDS = ['ind:humidity', 'ind:feelslike', 'out:humidity', 'out:feelslike']" in INDEX
    assert "id === 'ind:_ca'" in d


def test_per_sensor_lines_use_one_pattern_per_sensor_and_color_means_method():
    assert "const SENSOR_DASH = [[], [8, 3, 2, 3], [2, 3]" in INDEX
    for token in ("--c-ink", "--c-plan", "--c-base", "--c-v3", "--c-out", "--c-svc"):
        assert INDEX.count(token + ":") >= 2, token  # light and dark values
    ser = _func("_buildSeries")
    assert "color: 'plan'" in ser and "color: 'base'" in ser and "color: 'v3'" in ser and "color: 'svc'" in ser


def test_gaps_are_shown_as_gaps():
    assert "y: null" in _func("_withGaps")
    assert "spanGaps: false" in _func("_dsFor")


def test_v4_is_drawn_whenever_it_has_data_no_accuracy_gate():
    """Issue #1230 R6fe/X3 (owner decision O2: DP-8 is superseded, there is no accuracy gate).

    The chart draws v4 whenever the server says it has something to draw (``ready`` = "has data", not a verdict).
    The only two notes on an empty v4 line are that v4 is switched off or that it has no data for this zone yet;
    neither mentions an accuracy check. Issue #1459: those are notes on a switchable chip; only
    ``capabilities.v4_running`` false locks a v4 line. The accuracy page is a link for information only."""
    ser = _func("_buildSeries")
    assert "const v4Ready = v4.ready === true" in ser
    assert "v4Ready ? [...past, ...fut] : []" in ser and "v4Ready ? base : []" in ser and "v4Ready ? corr : []" in ser
    assert "'v4 is switched off (Settings, Prediction engine)'" in ser
    assert "'v4 has no data for this zone yet (it is still learning this house)'" in ser
    assert "const v4Lock = _capLock(cd, 'v4_running')" in ser and "disabledReason: v4Lock, note: why" in ser
    assert "accuracy check" not in ser
    key = _func("renderKey")
    assert '<a href="prediction_accuracy.html' in key and "How each engine has scored" in key
    assert "'?from=classic'" in key  # classic's link brings the scoring page back to classic
    # Issue #1459 (reverses the 0.9.6 Lane F lock): a chip with nothing to draw in the view keeps its reason but stays
    # switchable; only a lock (the home cannot have the line, from capabilities) disables it
    assert "disabled: !!lock" in key and "blockOf(s.id, s)" in key and "disabled: !!why" not in key
    assert "if (s) return s.disabledReason || ''" in _func("_chipLock")
    assert "if (s && s.note) return s.note" in _func("_chipEmpty")
    assert "accuracy check" not in key
    assert "chart-v4-note" in INDEX


def test_the_servers_v4_ready_is_has_data_not_an_accuracy_verdict():
    """The server half of the same rule: a fitted model whose published backtest scores badly is still ``ready``."""
    from datetime import UTC, datetime  # noqa: PLC0415

    from custom_components.climate_advisor import prediction_accuracy as pa  # noqa: PLC0415

    now = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)

    def _block(model, backtest):
        return pa.build_chart_v4(
            now=now,
            start=now.timestamp() - 86400,
            end=now.timestamp() + 86400,
            range_str="24h",
            unit="fahrenheit",
            historical=False,
            archive=None,
            backtest=backtest,
            model=model,
            last_future=None,
            sensor_ids=["climate.main"],
        )

    poor = {"gate_summary": {"passed": 0, "scored": 10, "total": 10}}  # v4 beat v3 on none of the rows
    out = _block({"has_model": True, "confidence": "low", "fitted_at": "2026-11-05T03:15:00+00:00"}, poor)
    assert out["ready"] is True and out["reason"] == "ok"
    assert out["scores"] == {"v4_best_rows": 0, "scored_rows": 10, "rows": 10}  # information only
    none = _block(None, None)
    assert none["ready"] is False and none["reason"] == "no_model"  # nothing to draw is the only "not ready"


def test_backtest_lead_is_selectable_and_defaults_to_four_hours():
    assert "let _chartLead = '4h'" in INDEX
    assert "[_chartLead]" in _func("_buildSeries")  # past v4 lines read the chosen lead
    assert "_leadRowHtml(v4, ready, _capLock(cd, 'v4_running'))" in _func("renderKey")
    assert "data-lead" in _func("_leadRowHtml") and "CHART_LEADS.map" in _func("_leadRowHtml")
    assert "_chartPrefs.lead = lead" in _func("setChartLead")


def test_choices_are_remembered_per_zone_in_try_catch_storage():
    assert "const CHART_PREFS_KEY = 'ca_chart_v2'" in INDEX
    assert "CHART_PREFS_KEY + ':' + (_host.zone() || 'default')" in _func("_prefsKey")
    read, write = _func("_readPrefs"), _func("_writePrefs")
    assert "try {" in read and "catch" in read and "try {" in write and "catch" in write
    assert "localStorage.setItem" in write and "localStorage.getItem" in read
    assert "_writePrefs()" in _func("toggleSeries") and "_writePrefs()" in _func("setChartRange")
    assert "_chartPrefsZone === zone" in _func("_ensurePrefs")  # switching zone loads that zone's choices


def test_hover_readout_lists_every_visible_value_and_the_forecast_miss():
    r = _func("_renderReadout")
    for group in ("Indoor", "Predicted", "Outdoor", "Overlays"):
        assert f"'{group}'" in r
    assert "missVs" in r and "chp-miss" in r and "isPast" in r
    assert "!_isOn(s.id) || s.disabledReason" in r  # nothing hidden is read out
    assert "_valueAt(" in r


def test_touch_tap_pins_the_readout_and_a_second_tap_lets_go():
    init = _func("_initChartInteraction")
    assert "'touchend'" in init and "_setCursor(ms, true)" in init and "_clearCursor()" in init
    assert "_multiTouch" in init  # a pinch is never read as a tap or swipe
    assert "_lastTouchAt" in init  # synthesised mouse events after a tap are ignored
    assert "clearChartPin()" in _func("_renderReadout")
    assert "ArrowLeft" in init and "Escape" in init


def test_key_folds_at_every_width_and_remembers_it():
    assert "<summary>Lines " in INDEX
    assert ".chart-key > summary { display: none; }" not in INDEX  # the fold control shows at every width
    assert '<details class="chart-key" id="chart-key">' in INDEX  # folded on a first visit
    s = _func("_syncKeyOpen")
    assert "_chartPrefs.keyOpen" in s and "max-width" not in s
    assert "_chartPrefs.keyOpen = key.open" in _func("_onKeyToggle")
    r = _func("_renderKeySummary")
    assert "aria-expanded" in r and "'Showing: '" in r
    assert "min-height: 36px" in INDEX  # touch targets


def test_table_view_is_an_accessible_table_of_the_visible_window():
    assert 'id="chart-table-toggle"' in INDEX and 'aria-pressed="false" onclick="toggleTableView()"' in INDEX
    t = _func("_applyTableView")
    assert "<caption>" in t and 'scope="col"' in t and 'scope="row"' in t
    m = _func("buildTableModel")
    assert "_viewWindow()" in m and "_isOn(s.id)" in m and "!s.disabledReason" in m
    assert 'role="region"' in INDEX and 'aria-label="Chart values as a table"' in INDEX
    assert "_copyText(" in _func("copyChartTable")  # copy and paste, with the execCommand fallback
    assert "_applyTableView()" in _func("toggleSeries") and "_applyTableView()" in _func("_applyWindow")


def test_units_come_from_the_payload():
    assert "unitSymbol(_lastChartData ? _lastChartData.unit" in _func("_unitSym")
    assert "'Temperature (' + _unitSym() + ')'" in INDEX
    assert "Temperatures in ' + escapeHtml(sym)" in _func("_applyTableView")
    assert "callback: v => v + '°'" in INDEX  # axis ticks carry the degree sign, the title the unit


def test_activity_strips_toggle_and_future_is_dimmer():
    s = _func("drawStrips")
    assert "_stripOn(r.id)" in s and "sg.future ? 0.4 : 1" in s
    seg = _func("_stripSegments")
    # Issue #1436: right of NOW every strip is a plan span (timeline `strips`), never chart_data.predicted_activity,
    # whose all-night "fan active / windows recommended" contradicted the plan's close step
    assert "_timeline.strips" in seg and "predicted_activity" not in seg
    assert "e.windows_planned" in seg  # the past Win plan is what the plan said at the time


def test_every_existing_overlay_is_kept():
    assert "label: 'Target Band'" in INDEX
    # Issue #1414: humidity / feels-like are series now (label 'Humidity' / 'Feels-like'), not overlay datasets
    assert "label: 'Humidity %'" not in INDEX and "label: 'Humidity'" in INDEX and "label: 'Feels-like'" in INDEX
    assert "label: 'Target'" in INDEX and "label: 'Thermostat set'" in INDEX
    assert "segment: { borderColor:" in INDEX  # heat / cool coloured target line
    assert "chartData.commanded_setpoint_history" in INDEX
    assert "effective_target_history" in INDEX and "effective_target_forecast" in INDEX
    assert "'override', 'classification_change'" in INDEX  # event markers


def test_reading_cards_keep_every_old_reading():
    cards = _func("renderReadingCards")
    for needle in (
        "Outdoor sensor",
        "_saysVs('The weather service'",
        "Today",
        "_trendWords(data.trend_direction",
        "Doors/Windows",
        "c.unreadable",
        "_fanLines(data)",
        "Occupancy",
        "Comfort score",
        "data.automation_status",
        "data.coordinator_healthy === false",
        "data.invariant_violations",
        "data.hvac_control_issues",
        "data.entity_health_issues",
        "pause_suppressed_classification_text",
        "nat_vent_off_threshold",
        "sleep_indoor_sensor_active",
        "remote-offset-line",
        "data.next_action",
        "data.next_automation_action",
        "data.next_automation_time",
        "hvac_runtime_today",
        "hvacNote",
    ):
        assert needle in cards, needle
    fans = _func("_fanLines")
    for needle in ("whf_status", "hvac_fan_status", "fan_status", "fan_remote_timer_hours", "fan_remote_speed"):
        assert needle in fans, needle
    assert "contact_sensors" in cards
    # the thermostat card is the one that carries "HVAC" (the older browser specs find it by that word), and its
    # line is the plain sentence (Issue #1399: "Heating to 68°F · idle now", not "HVAC heat · idle / 68°F")
    assert "Indoor · HVAC" in cards and "_hvacSentence(data, unitSym)" in cards
    assert 'id="status-next"' in INDEX and 'id="status-grid"' in INDEX


def test_only_bundled_libraries_load_and_the_old_chart_helpers_are_gone():
    srcs = re.findall(r'<script src="([^"]+)"', INDEX)
    assert srcs and all(not s.startswith(("http", "//")) for s in srcs)
    assert "drawActivityTimeline" not in INDEX and "_rangeToWindow" not in INDEX
    assert 'id="overlay-comfort"' not in INDEX


def test_accessibility_names_on_canvases_and_controls():
    for cid in ("temp-chart", "out-chart", "activity-chart"):
        assert re.search(rf'<canvas id="{cid}" role="img" aria-label="[^"]+"', INDEX), cid
    assert 'role="group" aria-label="Past: how much history to show"' in INDEX
    assert 'role="group" aria-label="Ahead: how much forecast to show"' in INDEX
    assert ".key-chip:focus-visible" in INDEX


def test_away_and_override_strips_read_the_chart_log_context_and_forecast_draws_nothing():
    assert "id: 'away'" in INDEX and "id: 'override'" in INDEX
    seg = _func("_stripSegments")
    assert "e.occupancy === 'away' || e.occupancy === 'vacation'" in seg and "e.override === true" in seg
    # Issue #1436: their faded part is the plan's setback / resume span (timeline_surfaces.strip_spans), not a forecast
    assert "sp.row !== r.id" in seg and "ftests" not in seg


def test_band_is_labelled_with_the_sensor_it_applies_to_in_key_and_hover():
    assert "function _bandLabelInView()" in INDEX and "'Band (' + names.join(' / ') + ')'" in INDEX
    assert "e.band_sensor" in _func("_bandLabelAt") and "bedroom sensor" in _func("_sensorLabel")
    assert "_bandLabelInView()" in _func("renderKey")
    assert "_bandLabelAt(ms)" in _func("_renderReadout")
