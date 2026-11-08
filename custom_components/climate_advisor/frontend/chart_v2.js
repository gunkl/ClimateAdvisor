/* Issue #1230 R2/R6fe: chart v2, shared by the classic Status tab (index.html) and the new dashboard.

   One implementation, two pages: both load chart.umd.min.js + hammer + the zoom plugin, then this file, and both put
   the same chart markup (temp-chart, out-chart, activity-chart, chart-key, chart-hover-panel ...) on the page.
   A page adapts it through the host object passed to CAChartV2.init():

     CAChartV2.init({
       zone():      the selected zone's entry id, or null
       api(path):   Promise of parsed JSON for /api/climate_advisor/<path> (zone param added by the page's helper);
                    a rejected promise carries .message ('HTTP 503' ...)
       onData(cd):  called after every successful chart_data load and draw (the page refreshes its own cards)
     })

   Then: loadChart(beforeTs), setStatus(statusPayload), data(), loadTimeline(), timeline(), renderPlanList(el, opts).
   The operation timeline (R3) is read-only here: the plan overlay (markers and path) and the "Next 24 hours" list
   draw what GET /api/climate_advisor/timeline returns; nothing is recomputed. Display only: nothing here decides
   anything. The inline onclick handlers of the chart markup call the window.* functions exported at the bottom. */
(function () {
  'use strict';

  let _host = {
    zone: function () { return null; },
    api: function () { return Promise.reject(new Error('CAChartV2 host not set')); },
    onData: function () {},
  };

  function unitSymbol(unit) {
    return unit === 'celsius' ? '°C' : '°F';
  }

  function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
  }

  function _copyText(text) {
    const fallback = () => {
      const ta = document.createElement('textarea');
      ta.value = text;
      ta.style.position = 'fixed';
      ta.style.opacity = '0';
      document.body.appendChild(ta);
      ta.select();
      document.execCommand('copy');
      document.body.removeChild(ta);
    };
    try {
      // writeText returns a promise that rejects without a secure context / permission (an HA iframe panel): use the
      // textarea fallback then, instead of leaving an unhandled rejection.
      const p = navigator.clipboard.writeText(text);
      if (p && typeof p.catch === 'function') p.catch(fallback);
    } catch (_e) {
      fallback();
    }
  }

  // --- Chart v2 (Issue #1230 R2) ---
  //
  // Occupant view: one readable chart on the Status tab. Indoor and outdoor are two charts on one time axis, every
  // indoor sensor (hallway, bedroom, the thermostat's finer sensor) has its own line so a line never jumps when
  // Climate Advisor swaps the sensor it reads, today's prediction (v3) sits beside v4's lines (whichever the occupant
  // selected in Settings is the main line, the other is an optional line), and a compact grouped key switches each line on or off. Choices are remembered in
  // this browser, per zone. The chart is display only: nothing here decides anything.
  //
  // Series catalog: _buildSeries() is the single list the charts, the key, the hover readout and the table view all
  // read, so a line can never be drawn without a key entry, a hover value and a table column (or the reverse).

  // Colours the chart still needs from the older palette: the heat / cool target line.
  const CHART_COLORS = {
    hvac_heating:  'rgba(239, 83, 80, 0.75)',    // red
    hvac_cooling:  'rgba(66, 165, 250, 0.60)',   // light blue
    target:        'rgba(255, 193, 7, 0.95)',    // amber/gold: the unified effective target
  };

  // Target line mode coloring: collapses both the historical vocabulary (state_log[].hvac: "heating"/"cooling"/...)
  // and the forecast vocabulary (predicted_activity[].hvac_mode: "heat"/"cool"/"off") to one {heat, cool, null} value.
  function _normalizeTargetMode(raw) {
    const s = (raw || '').toLowerCase();
    if (s === 'heating' || s === 'heat') return 'heat';
    if (s === 'cooling' || s === 'cool') return 'cool';
    return null;
  }

  function _targetModeColor(mode) {
    if (mode === 'heat') return CHART_COLORS.hvac_heating;
    if (mode === 'cool') return CHART_COLORS.hvac_cooling;
    return CHART_COLORS.target;
  }

  const CHART_RANGES = ['6h', '12h', '24h', '3d', '7d', '30d', '1y'];
  // Ahead: the forecast right of NOW. Never under a day, so the "now" view shows at least 24 h of future on every
  // range; a remembered shorter choice (the old '12h' button) reads back as 24h.
  const CHART_AHEADS = ['24h', '48h', '5d'];
  const MIN_AHEAD_H = 24;
  const CHART_LEADS = ['1h', '4h', '12h', '24h'];
  const CHART_HOURS = { '6h': 6, '12h': 12, '24h': 24, '48h': 48, '3d': 72, '5d': 120, '7d': 168, '30d': 720, '1y': 8760 };
  const CHART_PREFS_KEY = 'ca_chart_v2';
  // One line pattern per sensor (solid, dash-dot, dotted, long dash, short dash): identity is never colour alone.
  const SENSOR_DASH = [[], [8, 3, 2, 3], [2, 3], [10, 4], [4, 4]];
  const OUT_PX = 44;                  // fixed left axis width so the indoor and outdoor plot areas line up
  const RIGHT_PX = 40;                // fixed right gutter (kept for the humidity axis, Issue #1414)
  // Humidity and feels-like: no reading has ever been logged for them (chart_log has no humidity / feelslike), so
  // their chips are hidden on both dashboards until Issue #1414 wires them up; stored choices are dropped on read.
  const HIDDEN_OVERLAYS = ['ov:humidity', 'ov:feelslike'];

  let _tempChart = null;          // Chart.js instance: indoor chart (canvas #temp-chart)
  let _outChart = null;           // Chart.js instance: outdoor chart (canvas #out-chart)
  let _chartRange = '24h';        // past: how much history is in view
  let _chartAhead = '24h';        // ahead: how much future is in view
  let _chartLead = '4h';          // how long before each moment the v4 lines were made (left of NOW)
  let _lastChartData = null;      // cache for redraws
  let _lastStatus = null;         // last /status payload (the reading cards read both)
  let _chartZoomed = false;       // track zoom state
  let _zoomWin = null;            // {min, max} ms when zoomed, else null
  let _windowOffset = 0;          // ms scroll offset from "now at the centre of the window"
  let _dataWindowStart = null;    // earliest ts (ms) of currently loaded chart data
  let _dataWindowEnd   = null;    // latest ts (ms) of currently loaded chart data
  let _isHistoricalView = false;  // true when _lastChartData came from a before_ts fetch
  let _touchStartX = 0;           // touch start x for swipe navigation
  let _touchStartY = 0;
  let _touchStartAt = 0;
  let _multiTouch = false;        // a pinch is in progress: never read it as a swipe or a tap
  let _lastTouchAt = 0;           // mouse events synthesised after a tap are ignored
  let _chartPrefs = null;
  let _chartPrefsZone = '\u0000';
  let _cursorMs = null;           // hover / pinned readout time (ms); null = the readout rests on NOW (Issue #1408)
  let _readoutHeld = 0;           // tallest readout height (px) reached (NOW, hover or pin); 0 = not held
  let _readoutFresh = false;      // the hold was just released: size to the new content, not the old one
  let _readoutHeldW = 0;          // the readout's width when the height was held (a width change re-flows the grid)
  let _downX = null, _downY = null; // mousedown point: a click pins, a drag (zoom) does not
  let _cursorPinned = false;
  let _series = [];               // the catalog of the last draw
  let _v4Learning = [];           // indoor sensors v4 has nothing for yet (named on one muted line in the key)
  let _tableView = false;

  function _hoursOf(preset) { return CHART_HOURS[preset] || 24; }
  function _parseTs(ts) { return new Date(ts).getTime(); }

  // ---- remembered choices (per browser, per zone) ----
  function _prefsKey() { return CHART_PREFS_KEY + ':' + (_host.zone() || 'default'); }

  function _readPrefs() {
    const prefs = { on: {}, range: '24h', ahead: '24h', lead: '4h', table: false, keyOpen: false };
    try {
      const raw = window.localStorage.getItem(_prefsKey());
      if (!raw) return prefs;
      const p = JSON.parse(raw);
      if (p && typeof p === 'object') {
        if (p.on && typeof p.on === 'object') {
          Object.keys(p.on).forEach(k => { if (typeof p.on[k] === 'boolean') prefs.on[k] = p.on[k]; });
          // Humidity and feels-like are hidden until they are wired up (Issue #1414): drop their stored choices.
          HIDDEN_OVERLAYS.forEach(k => { if (k in prefs.on) { delete prefs.on[k]; prefs.migrated = true; } });
        }
        if (CHART_RANGES.includes(p.range)) prefs.range = p.range;
        if (CHART_AHEADS.includes(p.ahead)) prefs.ahead = p.ahead;
        else if (p.ahead !== undefined) prefs.migrated = true; // e.g. a stored '12h': 24h, re-saved on load
        if (CHART_LEADS.includes(p.lead)) prefs.lead = p.lead;
        prefs.table = p.table === true;
        prefs.keyOpen = p.keyOpen === true;
        _migrateEnginePrefs(prefs.on, p.model);
      }
    } catch (_e) { /* storage blocked or corrupt: start from the defaults */ }
    return prefs;
  }

  function _writePrefs() {
    try { window.localStorage.setItem(_prefsKey(), JSON.stringify(_chartPrefs)); } catch (_e) { /* private window */ }
  }

  function _ensurePrefs() {
    const zone = String(_host.zone() || 'default');
    if (_chartPrefs !== null && _chartPrefsZone === zone) return;
    _chartPrefs = _readPrefs();
    _chartPrefsZone = zone;
    if (_chartPrefs.migrated) { delete _chartPrefs.migrated; _writePrefs(); }
    _chartRange = _chartPrefs.range;
    _chartAhead = _chartPrefs.ahead;
    _chartLead = _chartPrefs.lead;
    _tableView = _chartPrefs.table;
    _windowOffset = 0;
    _zoomWin = null;
    _chartZoomed = false;
    _syncRangeButtons();
    _syncTableToggle();
    _syncKeyOpen();
  }

  // The baseline ("no plan") and the two optional overlays start off (DP-1). Of the indoor readings only the CA reading
  // starts on (it follows whichever sensor CA reads); every chip choice is remembered per browser, per zone.
  // Prediction engines (Settings -> Prediction engine): the PRIMARY engine's line is the main prediction, the optional
  // SHADOW engine's line is drawn for comparison, and any other engine's lines start off but stay in the key. While
  // the primary has no usable fit the main line is the plain schedule estimate (never the other engine).
  function _engines() {
    const pe = _lastChartData && _lastChartData.prediction_engines;
    return pe && typeof pe.primary === 'string' ? pe : { primary: 'v3', shadow: 'none', fallback: false, text: '' }; // an older backend: v3 is the line
  }
  function _shown(engine) { const e = _engines(); return e.primary === engine || e.shadow === engine; }
  function _v4HasData() { return !!(_lastChartData && _lastChartData.v4 && _lastChartData.v4.ready === true); }
  function _defaultOn(id) {
    if (id === 'v3') return _shown('v3');
    if (id === 'schedule') return !!_engines().fallback && _engines().primary !== 'v3';
    if (id.startsWith('v4:') || id === 'out:corrected') return _shown('v4') && _v4HasData();
    if (id.startsWith('base:')) return false;
    if (id === 'ov:humidity' || id === 'ov:feelslike') return false;
    if (id === 'ind:_ca') return true;
    // Each sensor's own line starts off: the CA reading is the one indoor line until you pick others (unless it has no data).
    if (id.startsWith('ind:')) return !((_lastChartData && _lastChartData.actual_indoor) || []).length;
    return true;
  }

  // Engine lines are remembered by ROLE (primary / shadow / other), not by engine, so switching engines in Settings
  // keeps the viewer's choices: "the main line off" stays off whichever engine is the main one. Stored keys are
  // 'role:<role>:<line>' with <line> = '_ca' for the CA-reading line (v3's only line), the sensor id for a v4 sensor
  // line, or 'out:corrected'. A v4 sensor line with no choice of its own follows its role's '_ca' choice.
  function _engineOf(id) { return id === 'v3' ? 'v3' : (id.startsWith('v4:') || id === 'out:corrected') ? 'v4' : null; }
  function _roleOf(engine, e) { return e.primary === engine ? 'primary' : e.shadow === engine ? 'shadow' : 'other'; }
  function _roleLine(id) { return id === 'v3' ? '_ca' : id === 'out:corrected' ? id : id.slice(3); }
  function _prefId(id, e) {
    const engine = _engineOf(id);
    return engine ? 'role:' + _roleOf(engine, e || _engines()) + ':' + _roleLine(id) : id;
  }
  // Older stores kept engine ids ('v3', 'v4:<sensor>', 'out:corrected') plus the pairing they were made under
  // ('model': '<primary>+<shadow>'): move them to their role then; without a pairing they cannot be placed, so drop them.
  function _migrateEnginePrefs(on, model) {
    const m = typeof model === 'string' ? model.split('+') : null;
    Object.keys(on).forEach(k => {
      if (!_engineOf(k)) return;
      if (m && m.length === 2) {
        const nk = _prefId(k, { primary: m[0], shadow: m[1] });
        if (typeof on[nk] !== 'boolean') on[nk] = on[k];
      }
      delete on[k];
    });
  }

  function _isOn(id) {
    if (HIDDEN_OVERLAYS.includes(id)) return false;   // Issue #1414: not offered until wired up
    const on = _chartPrefs ? _chartPrefs.on : {};
    const k = _prefId(id);
    let v = on[k];
    if (typeof v !== 'boolean' && id.startsWith('v4:')) v = on['role:' + _roleOf('v4', _engines()) + ':_ca'];
    return typeof v === 'boolean' ? v : _defaultOn(id);
  }

  function _cssVar(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || '#888';
  }

  // ---- time window ----
  function _baseWindow() {
    const centre = Date.now() + _windowOffset;
    const aheadH = Math.max(MIN_AHEAD_H, _hoursOf(_chartAhead));
    return { min: centre - _hoursOf(_chartRange) * 3600000, max: centre + aheadH * 3600000 };
  }
  function _viewWindow() { return _zoomWin || _baseWindow(); }
  // Scroll by half a view (the visible span, so a zoomed view scrolls in proportion).
  function _scrollStep() { const w = _baseWindow(); return (w.max - w.min) / 2; }

  // Smallest preset covering a span (a scrolled fetch must cover the whole window, past and ahead).
  function _coveringRange(spanHours) {
    for (const r of CHART_RANGES) { if (_hoursOf(r) >= spanHours) return r; }
    return '1y';
  }

  function _getDataMinTs(data) {
    const sources = [data.state_log, data.actual_indoor, data.actual_outdoor].filter(Boolean);
    let min = null;
    for (const arr of sources) {
      for (const e of arr) {
        const t = new Date(e.ts || e.time).getTime();
        if (!isNaN(t) && (min === null || t < min)) min = t;
      }
    }
    return min;
  }

  // Latest timestamp across historical and prediction arrays, so the user can scroll forward into the forecast.
  function _getDataMaxTs(data) {
    const sources = [
      data.state_log, data.actual_indoor, data.actual_outdoor,
      data.predicted_indoor, data.forecast_outdoor,
    ].filter(Boolean);
    let max = null;
    for (const arr of sources) {
      for (const e of arr) {
        const t = new Date(e.ts || e.time).getTime();
        if (!isNaN(t) && (max === null || t > max)) max = t;
      }
    }
    return max;
  }

  // Shift the window by direction (+1 / -1) x half a view. When the view drifts before the loaded data, re-fetch
  // anchored to its right edge; when it comes back near the present, drop the anchor and re-fetch live.
  function shiftChartWindow(direction) {
    _zoomWin = null;
    _chartZoomed = false;
    const resetBtn = document.getElementById('chart-zoom-reset');
    if (resetBtn) resetBtn.classList.remove('visible');
    const step = _scrollStep();
    _windowOffset += direction * step;
    const maxOffset = (_dataWindowEnd !== null) ? Math.max(0, _dataWindowEnd - Date.now()) : 0;
    if (_windowOffset > maxOffset) _windowOffset = maxOffset;
    const { min: vpStart, max: vpEnd } = _baseWindow();
    if (_dataWindowStart !== null && vpStart < _dataWindowStart) {
      loadChart(vpEnd);
      return;
    }
    if (_isHistoricalView && _dataWindowEnd !== null && vpEnd > _dataWindowEnd) {
      if (vpEnd >= Date.now() - step) {
        _windowOffset = 0;
        loadChart();
      } else {
        loadChart(vpEnd);
      }
      return;
    }
    if (_windowOffset < 0 && _windowOffset >= -step / 2 && _dataWindowEnd !== null) {
      _windowOffset = 0;
      loadChart();
      return;
    }
    if (_lastChartData) drawChart(_lastChartData);
  }

  function chartGoNow() {
    _windowOffset = 0;
    _zoomWin = null;
    _chartZoomed = false;
    const resetBtn = document.getElementById('chart-zoom-reset');
    if (resetBtn) resetBtn.classList.remove('visible');
    loadChart();
  }

  // ---- the series catalog ----
  function _medianStep(pts) {
    const d = [];
    for (let i = 1; i < pts.length; i++) {
      const g = pts[i].x - pts[i - 1].x;
      if (g > 0) d.push(g);
    }
    if (!d.length) return 30 * 60000;
    d.sort((a, b) => a - b);
    return d[d.length >> 1];
  }

  // Gaps are shown as gaps: a null point breaks the line where readings are missing for a few steps.
  function _withGaps(pts, step) {
    const limit = Math.max(3.5 * step, 100 * 60000);
    const out = [];
    for (let i = 0; i < pts.length; i++) {
      if (i > 0 && pts[i].x - pts[i - 1].x > limit) out.push({ x: pts[i - 1].x + 1, y: null });
      out.push(pts[i]);
    }
    return out;
  }

  // A break (ms) ends the line just before that moment: the next point starts a new segment (Issue #1400: the CA
  // reading swaps sensor at bedtime / wake-up, and no segment may cross a swap).
  function _withBreaks(data, breaks) {
    if (!breaks || !breaks.length || data.length < 2) return data;
    const marks = breaks.filter(b => !isNaN(b) && b > data[0].x && b <= data[data.length - 1].x).sort((a, b) => a - b);
    if (!marks.length) return data;
    const out = [];
    let k = 0;
    for (const p of data) {
      while (k < marks.length && marks[k] <= p.x) { out.push({ x: marks[k] - 1, y: null }); k++; }
      out.push(p);
    }
    return out;
  }

  function _mkSeries(def, pts) {
    const sorted = pts.filter(p => p.y != null && !isNaN(p.x)).sort((a, b) => a.x - b.x);
    const step = _medianStep(sorted);
    return Object.assign({
      width: 2, dash: [], chart: 'in', kind: 'line', disabledReason: '',
    }, def, { raw: sorted, data: _withBreaks(_withGaps(sorted, step), def.breaks), tol: Math.max(45 * 60000, 1.5 * step) });
  }

  function _buildSeries(cd) {
    const nowMs = Date.now();
    const S = [];
    const metas = cd.indoor_sensors || [];
    const v4 = cd.v4 || {};
    const v4Ready = v4.ready === true;
    const state = cd.state_log || [];
    // Every line that follows the reading CA uses (actual, v3, v4) is broken at its sensor swaps (Issue #1400).
    const caBreaks = (cd.ca_swaps || ((v4.breaks || {})._ca) || []).map(_parseTs); // top-level: any engine (older: v4's)

    // Indoor: one actual line per sensor, then the CA reading (follows the sensor CA reads).
    metas.forEach((m, i) => {
      const pts = ((cd.actual_indoor_sensors || {})[m.id] || []).map(p => ({ x: _parseTs(p.time), y: p.temp }));
      S.push(_mkSeries({ id: 'ind:' + m.id, group: 'indoor', short: m.name, label: m.name, role: m.role,
        color: 'ink', dash: SENSOR_DASH[i % SENSOR_DASH.length], sensor: m.id, kind: 'actual' }, pts));
    });
    const caPts = (cd.actual_indoor || []).map(p => ({ x: _parseTs(p.time), y: p.temp }));
    S.push(_mkSeries({ id: 'ind:_ca', group: 'indoor', short: 'CA reading', label: 'CA reading',
      role: 'the reading Climate Advisor uses', color: 'ink', dash: [1, 3], width: 1.5, sensor: '_ca', kind: 'actual', breaks: caBreaks }, caPts));

    // Predicted: today's prediction (v3): the 4 h-ahead snapshots logged earlier, then the live forecast.
    const histPredIndoor = state.map(e => ({ x: _parseTs(e.ts), y: e.pred_indoor != null ? e.pred_indoor : e.pred_indoor_avg != null ? e.pred_indoor_avg : null }))
      .filter(p => p.y != null && p.x <= nowMs);
    const futPredIndoor = (cd.predicted_indoor || []).map(p => ({ x: _parseTs(p.ts), y: p.temp })).filter(p => p.x > nowMs);
    S.push(_mkSeries({ id: 'v3', group: 'predicted', short: 'v3', label: "Today's prediction (v3)", role: 'made 4 h ahead on the left of NOW',
      color: 'v3', dash: [7, 4], width: 2, missVs: 'ind:_ca', kind: 'pred', breaks: caBreaks }, [...histPredIndoor, ...futPredIndoor]));

    // The schedule estimate: the main line while the primary engine is still learning.
    const sched = (cd.schedule_estimate || []).map(p => ({ x: _parseTs(p.ts), y: p.temp })).filter(p => p.x > nowMs);
    S.push(_mkSeries({ id: 'schedule', group: 'predicted', short: 'Schedule estimate', label: 'Schedule estimate (learning)',
      role: 'shown while the main prediction engine is still learning', color: 'base', dash: [4, 3], width: 2, missVs: 'ind:_ca', kind: 'pred',
      disabledReason: sched.length ? '' : 'Not needed: the main engine has a usable fit' }, sched));

    // v4: drawn whenever it has data; there is no accuracy gate (which model is the main line is the occupant's setting).
    const why = v4Ready ? '' : (_engines().engines || []).some(x => x.id === 'v4' && !x.running)
      ? 'v4 is switched off (Settings, Prediction engine)' : 'v4 has no data for this zone yet (it is still learning this house)';
    // 0.9.6 Lane F: a v4 chip only for a sensor v4 has something for (past, forecast or no-plan line); the sensors v4
    // has not learned yet are named on one muted line instead (_v4Learning). A sensor with past predictions only (the
    // backtest) and no forecast is labelled "history only" on both its chips.
    _v4Learning = [];
    // v4.past only covers the fetched window; past_span.sensors lists every line with a past prediction anywhere, so a
    // short or scrolled view does not take a sensor's chips away (they are disabled "not in this view" instead).
    const spanSensors = (v4.past_span && v4.past_span.sensors) || [];
    metas.forEach((m, i) => {
      const dash = SENSOR_DASH[i % SENSOR_DASH.length];
      const byLead = (v4.past || {})[m.id] || {};
      const pastAny = CHART_LEADS.some(l => (byLead[l] || []).length > 0);
      const futRaw = (v4.future || {})[m.id] || [];
      const baseRaw = (v4.baseline || {})[m.id] || [];
      const pastElsewhere = spanSensors.includes(m.id);
      if (!v4Ready || (!pastAny && !pastElsewhere && !futRaw.length && !baseRaw.length)) { _v4Learning.push(m); return; }
      const histOnly = !futRaw.length && !baseRaw.length;
      const label = m.name + (histOnly ? ' (history only)' : '');
      const past = (byLead[_chartLead] || []).map(p => ({ x: _parseTs(p.ts), y: p.temp }));
      const fut = futRaw.map(p => ({ x: _parseTs(p.ts), y: p.temp }));
      S.push(_mkSeries({ id: 'v4:' + m.id, group: 'predicted', sub: 'plan', short: 'v4 ' + m.name, label, color: 'plan', dash,
        width: 2.5, sensor: m.id, missVs: 'ind:' + m.id, kind: 'pred', disabledReason: why, histOnly, pastElsewhere,
        role: histOnly ? 'past v4 predictions only (from its backtest); v4 does not forecast ' + m.name + ' yet' : '' }, v4Ready ? [...past, ...fut] : []));
      const base = baseRaw.map(p => ({ x: _parseTs(p.ts), y: p.temp }));
      S.push(_mkSeries({ id: 'base:' + m.id, group: 'predicted', sub: 'base', short: 'No plan ' + m.name, label, color: 'base', dash,
        width: 2, sensor: m.id, kind: 'pred', histOnly,
        disabledReason: why || (base.length ? '' : histOnly
          ? 'No forecast for ' + m.name + ' yet: v4 has only its past predictions, and the no-plan line is a forecast'
          : 'v4 has no no-plan line for ' + m.name + ' yet') }, v4Ready ? base : []));
    });

    // v4's line for the reading Climate Advisor uses (Issue #1400): the only line of a _ca-only model, and the one to
    // compare with today's (v3) line. It follows the sensor CA reads, so it is broken at every swap (v4.breaks._ca).
    if ((v4.sensors || []).includes('_ca')) {
      const caName = 'Climate Advisor reading';
      const past = (((v4.past || {})._ca || {})[_chartLead] || []).map(p => ({ x: _parseTs(p.ts), y: p.temp }));
      const fut = ((v4.future || {})._ca || []).map(p => ({ x: _parseTs(p.ts), y: p.temp }));
      S.push(_mkSeries({ id: 'v4:_ca', group: 'predicted', sub: 'plan', short: 'v4 ' + caName, label: caName, color: 'plan', dash: [],
        width: 2.5, sensor: '_ca', missVs: 'ind:_ca', kind: 'pred', disabledReason: why, breaks: caBreaks }, v4Ready ? [...past, ...fut] : []));
      const base = ((v4.baseline || {})._ca || []).map(p => ({ x: _parseTs(p.ts), y: p.temp }));
      S.push(_mkSeries({ id: 'base:_ca', group: 'predicted', sub: 'base', short: 'No plan ' + caName, label: caName, color: 'base', dash: [],
        width: 2, sensor: '_ca', kind: 'pred', disabledReason: why, breaks: caBreaks }, v4Ready ? base : []));
    }

    // Outdoor chart: the sensor, the weather service, and v4's corrected forecast.
    const outPts = (cd.actual_outdoor || []).map(p => ({ x: _parseTs(p.time), y: p.temp }));
    S.push(_mkSeries({ id: 'out:sensor', group: 'outdoor', short: 'Outdoor sensor', label: 'Sensor', color: 'out', width: 2, chart: 'out', kind: 'actual' }, outPts));
    const histSvc = state.map(e => ({ x: _parseTs(e.ts), y: e.pred_outdoor != null ? e.pred_outdoor : e.pred_outdoor_avg != null ? e.pred_outdoor_avg : null }))
      .filter(p => p.y != null && p.x <= nowMs);
    const futSvc = (cd.forecast_outdoor || []).map(p => ({ x: _parseTs(p.ts), y: p.temp })).filter(p => p.x > nowMs);
    S.push(_mkSeries({ id: 'out:service', group: 'outdoor', short: 'Service', label: 'Service forecast', color: 'svc', dash: [2, 3], width: 2,
      chart: 'out', missVs: 'out:sensor', kind: 'pred' }, [...histSvc, ...futSvc]));
    const co = v4.corrected_outdoor || {};
    const corr = [...(co.past || []), ...(co.future || [])].map(p => ({ x: _parseTs(p.ts), y: p.temp }));
    S.push(_mkSeries({ id: 'out:corrected', group: 'outdoor', short: 'Corrected', label: 'CA-corrected forecast', color: 'svc', width: 2.5,
      chart: 'out', missVs: 'out:sensor', kind: 'pred', disabledReason: why }, v4Ready ? corr : []));
    return S;
  }

  function _seriesById(id) { return _series.find(s => s.id === id) || null; }

  // ---- reading helpers ----
  function _valueAt(s, ms) {
    const pts = s.data;
    const n = pts.length;
    if (!n) return null;
    const tol = s.tol;
    if (ms < pts[0].x) return (pts[0].x - ms <= tol) ? pts[0].y : null;
    if (ms > pts[n - 1].x) return (ms - pts[n - 1].x <= tol && pts[n - 1].y != null) ? pts[n - 1].y : null;
    let lo = 0, hi = n - 1;
    while (lo < hi - 1) {
      const mid = (lo + hi) >> 1;
      if (pts[mid].x <= ms) lo = mid; else hi = mid;
    }
    const a = pts[lo], b = pts[hi];
    if (a.y == null || b.y == null) return null;       // inside a gap
    if (ms === a.x || b.x === a.x) return a.y;
    if (b.x - a.x > tol * 2) {
      const d0 = ms - a.x, d1 = b.x - ms;
      return Math.min(d0, d1) <= tol / 2 ? (d0 <= d1 ? a.y : b.y) : null;
    }
    return a.y + ((ms - a.x) / (b.x - a.x)) * (b.y - a.y);
  }

  // A past band row is the band in effect from its time until the next row: hold it flat and change at the next
  // row, never a slope between rows (a 30-minute Away recorded on one row drew a triangle, not a block).
  function _stepHeld(pts) {
    const sorted = pts.slice().sort((a, b) => a.x - b.x);
    const out = [];
    sorted.forEach((p, i) => {
      const prev = sorted[i - 1];
      if (prev && prev.y !== p.y) out.push({ x: p.x, y: prev.y });
      out.push(p);
    });
    return out;
  }

  function _fmt(v, digits) { return v == null ? '—' : v.toFixed(digits == null ? 1 : digits); }
  function _fmtSigned(v) { return (v > 0 ? '+' : v < 0 ? '−' : '') + Math.abs(v).toFixed(1); }
  function _unitSym() { return unitSymbol(_lastChartData ? _lastChartData.unit : (_lastStatus ? _lastStatus.unit : null)); }

  // ---- the key (compact, grouped, independent toggles) ----
  function _swatch(s) {
    const color = 'var(--c-' + s.color + ')';
    const dash = (s.dash || []).join(' ');
    return '<svg class="key-sw" width="30" height="10" aria-hidden="true"><line x1="2" y1="5" x2="28" y2="5" stroke="' + color +
      '" stroke-width="' + Math.min(3, s.width) + '"' + (dash ? ' stroke-dasharray="' + dash + '"' : '') + ' stroke-linecap="round"/></svg>';
  }

  function _chip(id, inner, opts) {
    const o = opts || {};
    const on = _isOn(id) && !o.disabled;
    return '<button type="button" class="key-chip' + (on ? ' on' : '') + (o.noData ? ' no-data' : '') + '" data-series="' + escapeHtml(id) +
      '" aria-pressed="' + (on ? 'true' : 'false') + '"' + (o.disabled ? ' disabled' : '') +
      (o.title ? ' title="' + escapeHtml(o.title) + '"' : '') + (o.aria ? ' aria-label="' + escapeHtml(o.aria) + '"' : '') + '>' + inner + '</button>';
  }

  function _group(label, ariaLabel, inner) {
    return '<div class="key-group" role="group" aria-label="' + escapeHtml(ariaLabel) + '"><span class="key-group-label">' + escapeHtml(label) + '</span>' +
      '<div class="key-chips">' + inner + '</div></div>';
  }

  const STRIP_ROWS = [
    // `short` is the label on the strip itself (the plot starts about 44 px from the edge); `label` names the key chip.
    { id: 'heating', label: 'Heating', short: 'Heating', color: 'rgba(239,83,80,0.85)' },
    { id: 'cooling', label: 'Cooling', short: 'Cooling', color: 'rgba(66,165,250,0.85)' },
    { id: 'fan', label: 'Fan', short: 'Fan', color: 'rgba(38,166,154,0.85)' },
    { id: 'natvent', label: 'Nat-vent', short: 'Nat-vent', color: 'rgba(102,187,106,0.85)' },
    { id: 'windows', label: 'Windows open', short: 'Windows', color: 'rgba(56,142,60,0.85)' },
    { id: 'windowsplan', label: 'Windows plan', short: 'Win plan', color: 'rgba(156,204,101,0.9)' },
    // Chart-log context fields (Issue #1230 R2): rows without them draw nothing.
    { id: 'away', label: 'Away / Vacation', short: 'Away', color: 'rgba(171,71,188,0.85)' },
    { id: 'override', label: 'Override / grace', short: 'Override', color: 'rgba(255,112,67,0.9)' },
  ];

  function _stripOn(id) { return _isOn('strip:' + id); }

  // The comfort band applies to whichever sensor Climate Advisor was reading (chart_log band_sensor): say which.
  function _sensorLabel(sid) {
    const m = ((_lastChartData || {}).indoor_sensors || []).find(x => x.id === sid);
    return m ? m.name + (m.role === 'bedroom sensor' ? ', sleep' : '') : null;
  }

  function _bandLabelAt(ms) {
    const log = ((_lastChartData || {}).state_log || []).filter(e => e.band_sensor);
    if (!log.length) return null;
    let best = null, bestD = Infinity;
    log.forEach(e => { const d = Math.abs(_parseTs(e.ts) - Math.min(ms, Date.now())); if (d < bestD) { bestD = d; best = e; } });
    return best && bestD < 3 * 3600000 ? _sensorLabel(best.band_sensor) : null;
  }

  function _bandLabelInView() {
    const w = _viewWindow();
    const names = [];
    ((_lastChartData || {}).state_log || []).forEach(e => {
      if (!e.band_sensor) return;
      const t = _parseTs(e.ts);
      if (t < w.min || t > w.max) return;
      const n = _sensorLabel(e.band_sensor);
      if (n && !names.includes(n)) names.push(n);
    });
    return names.length ? 'Band (' + names.join(' / ') + ')' : 'Target band';
  }

  // ---- what a chip would draw (0.9.6 Lane F: every chip that cannot draw anything is disabled and says why) ----
  //
  // Occupant view: switching a line on that has nothing to draw used to do nothing at all. Now a chip is switched off
  // and disabled when its line has no point in the current view, with the reason on hover (title) and for screen
  // readers (aria-label): no data at all, or data only at other times ("scroll or change the range"). The test reads
  // the datasets actually handed to Chart.js, so it is the same data that would be drawn.
  function _ptsDraw(pts, win) {
    for (let i = 0; i < pts.length; i++) {
      const p = pts[i];
      if (p.y == null || isNaN(p.x)) continue;
      if (p.x >= win.min && p.x <= win.max) return true;
      const q = pts[i + 1];
      if (q && q.y != null && p.x < win.min && q.x > win.max) return true;   // one segment spans the whole view
    }
    return false;
  }
  function _ptsAny(pts) { return pts.some(p => p.y != null && !isNaN(p.x)); }
  function _chipDatasets(id) {
    const out = [];
    [_tempChart, _outChart].forEach(ch => { if (ch) ch.data.datasets.forEach(d => { if (d.caId === id) out.push(d); }); });
    return out;
  }
  const _NOT_IN_VIEW = 'Nothing to draw in this view (it has data at other times): scroll or change the range';
  // '' = draws something in this view; else the reason it cannot. null = not known yet (no chart drawn yet).
  function _chipBlock(id, s) {
    if (s && s.disabledReason) return s.disabledReason;
    const win = _viewWindow();
    if (id.startsWith('strip:')) {
      const r = id.slice(6);
      if (r === 'events') {
        const ev = ((_lastChartData || {}).state_log || []).filter(e => e.event === 'override' || e.event === 'classification_change')
          .map(e => _parseTs(e.ts));
        if (ev.some(t => t >= win.min && t <= win.max)) return '';
        return ev.length ? _NOT_IN_VIEW : 'No overrides or re-classifications recorded in the loaded time';
      }
      const segs = _stripSegs[r] || [];
      if (segs.some(g => g.b > win.min && g.a < win.max)) return '';
      const row = STRIP_ROWS.find(x => x.id === r);
      return segs.length ? _NOT_IN_VIEW : 'No ' + (row ? row.label.toLowerCase() : r) + ' recorded or planned in the loaded time';
    }
    if (id === 'ov:plan') {
      const steps = _planSteps(5 * 86400000).filter(p => p._ms > Date.now());
      const path = _planPath();
      if (steps.some(p => p._ms >= win.min && p._ms <= win.max) || _ptsDraw(path, win)) return '';
      return steps.length || path.length ? 'The plan covers Now and the next days: not in this view'
        : (_timelineState === 'none' ? 'No plan yet' : 'No plan steps to show');
    }
    if (!_tempChart && !_outChart) return null;
    const dss = _chipDatasets(id);
    if (dss.some(d => _ptsDraw(d.data || [], win))) return '';
    if (s && s.sub === 'plan') {
      // a v4 line whose past exists only for other look-aheads in this view
      const byLead = (((_lastChartData || {}).v4 || {}).past || {})[s.sensor] || {};
      const others = CHART_LEADS.filter(l => l !== _chartLead &&
        _ptsDraw((byLead[l] || []).map(p => ({ x: _parseTs(p.ts), y: p.temp })), win));
      if (others.length) return 'No v4 predictions made ' + _chartLead.replace('h', ' h') + ' earlier in this view: choose ' +
        others.map(l => l.replace('h', ' h')).join(' or ') + ' under "Past v4 lines"';
    }
    if (dss.some(d => _ptsAny(d.data || [])) || (s && s.pastElsewhere)) return _NOT_IN_VIEW;
    if (id === 'ov:band') return 'No target band recorded or scheduled in the loaded time';
    if (id === 'ov:target') return 'No target recorded or scheduled in the loaded time';
    if (s && s.kind === 'actual') return 'No readings from ' + s.label + ' in the loaded time';
    return 'No data for this line in the loaded time';
  }

  // The past-lead row: the look-ahead buttons, or one line saying why there is nothing to choose (Issue #1230 R2;
  // 0.9.6 Lane F: "none in this view" vs "none yet"). The payload's v4.past only holds the fetched window; v4.past_span
  // (every past prediction this zone has) tells the two apart. An older backend without it: "yet" only for a live view.
  function _leadRowHtml(v4, ready) {
    const win = _viewWindow();
    const pastPts = l => Object.values((v4 && v4.past) || {}).flatMap(byLead => ((byLead || {})[l] || []))
      .map(p => ({ x: _parseTs(p.ts), y: p.temp }));
    const leadInView = l => ready && _ptsDraw(pastPts(l), win);
    const anyInView = CHART_LEADS.some(leadInView);
    const leadTxt = l => l.replace('h', ' h');
    const leadHelp = 'Left of Now, each v4 line shows what v4 predicted that many hours earlier for each moment, so you can ' +
      'see how well it called it. This changes only the past part of the v4 lines; from Now on they stay the same.';
    let inner;
    if (anyInView) {
      inner = '<div class="seg" role="group" aria-labelledby="chart-lead-label" aria-describedby="chart-lead-help">' +
        CHART_LEADS.map(l => {
          const has = leadInView(l);
          return '<button type="button" class="seg-btn' + (l === _chartLead ? ' on' : '') + '" data-lead="' + l +
            '" aria-pressed="' + (l === _chartLead ? 'true' : 'false') + '"' + (has ? '' : ' disabled') +
            ' title="' + (has ? 'Left of Now, show what v4 predicted ' + leadTxt(l) + ' earlier for each moment'
              : 'No predictions v4 made ' + leadTxt(l) + ' earlier in this view') + '">' +
            leadTxt(l) + ' earlier' + (has ? '' : ' (none in view)') + '</button>';
        }).join('') +
        '</div><span class="key-sub" id="chart-lead-help">how far ahead v4 looked; changes only the v4 lines left of Now</span>';
    } else {
      const span = v4 && v4.past_span;
      const loaded = ready && CHART_LEADS.some(l => pastPts(l).length > 0);
      const fmtDay = iso => new Date(iso).toLocaleString([], { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' });
      let msg, state;
      if (span && span.first && span.last) {
        msg = 'No past v4 predictions in this view; v4 has them from ' + fmtDay(span.first) + ' to ' + fmtDay(span.last);
        state = 'view';
      } else if (loaded || (v4 && !('past_span' in v4) && _isHistoricalView)) {
        msg = 'No past v4 predictions in this view';
        state = 'view';
      } else {
        msg = 'v4 has no past predictions to show yet';
        state = 'yet';
      }
      inner = '<span class="key-sub" id="chart-lead-help" data-lead-empty="' + state + '">' + escapeHtml(msg) + '</span>';
    }
    return '<div class="key-row" id="chart-lead-row" title="' + escapeHtml(leadHelp) + '">' +
      '<span class="key-sub" id="chart-lead-label">Past v4 lines: predicted</span>' + inner + '</div>';
  }

  function renderKey(cd, S) {
    const body = document.getElementById('chart-key-body');
    if (!body) return;
    const v4 = cd.v4 || null;
    const block = {};
    const blockOf = (id, s) => { if (!(id in block)) block[id] = _chipBlock(id, s); return block[id]; };
    const sw = s => _swatch(s);
    const chipFor = s => {
      const why = blockOf(s.id, s);
      return _chip(s.id, sw(s) + '<span>' + escapeHtml(s.label) + '</span>', {
        noData: why == null ? s.raw.length === 0 : !!why, disabled: !!why, title: why || s.role || '',
        aria: s.label + (s.sub === 'plan' ? ' with plan, v4' : s.sub === 'base' ? ' with no plan, v4' : '') + (why ? ': ' + why : ''),
      });
    };
    // an overlay or strip chip: the same rule, with the reason on hover and in its accessible name
    const ovChip = (id, inner, label, title) => {
      const why = blockOf(id, null);
      return _chip(id, inner, { noData: !!why, disabled: !!why, title: why || title || '', aria: why ? label + ': ' + why : '' });
    };
    const ind = S.filter(s => s.group === 'indoor');
    const indoorHtml = ind.filter(s => s.id !== 'ind:_ca' || s.raw.length > 0).map(chipFor).join('');
    const v3 = S.find(s => s.id === 'v3');
    const sched = S.find(s => s.id === 'schedule');
    const plan = S.filter(s => s.sub === 'plan');
    const base = S.filter(s => s.sub === 'base');
    const eng = _engines();
    const shadowTxt = eng.shadow && eng.shadow !== 'none' ? 'Comparison: ' + eng.shadow : 'No comparison engine';
    let predHtml = '<div class="key-row" id="chart-main-model"><span class="key-sub">Main</span><span>' +
      escapeHtml('Main prediction: ' + eng.primary + ' · ' + shadowTxt) + ' — change it in <a href="new_dashboard.html#settings">Settings → Prediction engine</a>.' +
      (eng.text ? ' ' + escapeHtml(eng.text) + '.' : '') + ' <a href="prediction_accuracy.html' + (/new_dashboard\.html$/.test(location.pathname) ? '' : '?from=classic') + // classic: its Back returns to index.html
      '">How each engine has scored</a>.</span></div>' +
      '<div class="key-row"><span class="key-sub">Today</span>' + chipFor(v3) + (sched && sched.raw.length > 0 ? chipFor(sched) : '') + '</div>';
    const ready = !!(v4 && v4.ready);
    if (plan.length) {
      predHtml += '<div class="key-row"><span class="key-sub">v4 with plan</span>' + plan.map(chipFor).join('') + '</div>';
      predHtml += '<div class="key-row"><span class="key-sub">v4 no plan</span>' + base.map(chipFor).join('') + '</div>';
      predHtml += _leadRowHtml(v4, ready);
    }
    // One muted line for the sensors v4 has nothing for yet (no chips for them: they would draw nothing).
    const v4Off = (eng.engines || []).some(x => x.id === 'v4' && !x.running);
    if (_v4Learning.length || (!ready && _shown('v4'))) {
      // the sensor's role when it says which one ("thermostat", "bedroom sensor"), else its name
      const KNOWN = ['thermostat', 'bedroom sensor', "thermostat's finer sensor"];
      const names = [...new Set(_v4Learning.map(m => KNOWN.includes(m.role) ? m.role : m.name))];
      // v4.reason says why there is nothing at all: switched off, a failed read (back on the next refresh), or learning
      const reason = (v4 && v4.reason) || '';
      const text = !ready && (v4Off || reason === 'not_running') ? 'v4 is switched off (Settings, Prediction engine)'
        : !ready && reason === 'error' ? "v4's lines could not be read this time; they come back on the next refresh"
        : 'v4 is still learning' + (names.length ? ': ' + names.join(', ') : ' this house');
      const title = _v4Learning.map(m => m.name + (m.role ? ' (' + m.role + ')' : '')).join(', ');
      predHtml += '<div class="key-row key-learning" id="chart-v4-learning"' + (title ? ' title="' + escapeHtml('No v4 lines yet for: ' + title) + '"' : '') +
        '><span class="key-sub">' + escapeHtml(text) + '</span></div>';
    }
    const outHtml = S.filter(s => s.group === 'outdoor').map(chipFor).join('');
    const ovSw = (color, dash, w) => '<svg class="key-sw" width="30" height="10" aria-hidden="true"><line x1="2" y1="5" x2="28" y2="5" stroke="' + color + '" stroke-width="' + w + '"' + (dash ? ' stroke-dasharray="' + dash + '"' : '') + ' stroke-linecap="round"/></svg>';
    // Humidity and feels-like are not offered until they are wired up (Issue #1414; HIDDEN_OVERLAYS).
    const bandLabel = _bandLabelInView();
    const overlays =
      ovChip('ov:band', '<svg class="key-sw" width="30" height="10" aria-hidden="true"><rect x="2" y="1" width="26" height="8" rx="2" fill="var(--c-band)" stroke="var(--c-bandline)" stroke-dasharray="3 2"/></svg><span>' + escapeHtml(bandLabel) + '</span>', bandLabel) +
      ovChip('ov:target', ovSw('var(--c-target)', '', 3) + '<span>Target</span>', 'Target') +
      ovChip('ov:plan', '<svg class="key-sw" width="30" height="10" aria-hidden="true"><circle cx="8" cy="5" r="3.5" fill="var(--c-plan)"/><path d="M22 0 L27 5 L22 10 L17 5 Z" fill="var(--c-target)" stroke="var(--c-ink)" stroke-width="1"/></svg><span>Plan</span>',
        'Plan', 'What Climate Advisor will do (circles) and what you are asked to do (diamonds), from the plan');
    const stripChips = STRIP_ROWS.map(r => ovChip('strip:' + r.id,
      '<svg class="key-sw" width="14" height="10" aria-hidden="true"><rect x="1" y="1" width="12" height="8" rx="2" fill="' + r.color + '"/></svg><span>' + r.label + '</span>', r.label)).join('') +
      ovChip('strip:events', '<svg class="key-sw" width="14" height="10" aria-hidden="true"><line x1="7" y1="0" x2="7" y2="10" stroke="#f44336" stroke-width="1.5" stroke-dasharray="3 2"/></svg><span>Events</span>', 'Events');
    body.innerHTML =
      _group('Indoor', 'Indoor lines', indoorHtml) +
      _group('Predicted', 'Prediction lines', predHtml) +
      _group('Outdoor', 'Outdoor lines', outHtml) +
      _group('Overlays', 'Overlays', overlays) +
      _group('Activity', 'Activity strips', stripChips);

    const note = document.getElementById('chart-v4-note');
    if (note) {
      const show = !!eng.fallback && !!eng.text;
      note.hidden = !show;
      if (show) {
        note.textContent = eng.text + (eng.control && eng.control.text ? ' ' + eng.control.text : '');
        note.setAttribute('data-engine-state', eng.fitting ? 'fitting' : 'learning');
      }
    }
    // The one-line summary of what is drawn (shown while the key is folded): the lines on, then the overlays on.
    _keyShown = S.filter(s => _isOn(s.id) && !blockOf(s.id, s) && s.raw.length > 0).map(s => s.short)
      .concat([...body.querySelectorAll('[data-series^="ov:"][aria-pressed="true"]:not(.no-data)')].map(b => b.textContent.trim()));
    _renderKeySummary();
  }

  let _keyShown = [];
  function _renderKeySummary() {
    const key = document.getElementById('chart-key');
    const sum = key && key.querySelector('summary');
    if (!sum) return;
    const n = _keyShown.length;
    const text = key.open ? 'Lines' : 'Showing: ' + (n ? _keyShown.join(' · ') : 'nothing');
    sum.setAttribute('aria-expanded', key.open ? 'true' : 'false');
    sum.title = key.open ? 'Hide the lines and sensors key' : 'Choose which lines and sensors the chart shows';
    sum.innerHTML = '<span class="key-sum-text">' + escapeHtml(text) + '</span> <span id="chart-key-count">(' + n + (key.open ? ' shown' : '') + ')</span>' +
      '<span class="key-sum-act" aria-hidden="true">' + (key.open ? '▾ Hide' : '▸ Edit') + '</span>';
  }

  // ---- drawing ----
  let _stripHasData = {};
  let _stripSegs = {};

  function _spanMs(w) { return w.max - w.min; }

  function _xTick(ms) {
    const w = _viewWindow();
    const d = new Date(ms);
    const span = _spanMs(w);
    if (span <= 36 * 3600000) return d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
    if (span <= 8 * 86400000) return d.toLocaleDateString([], { weekday: 'short' }) + ' ' + d.toLocaleTimeString([], { hour: 'numeric' });
    return d.toLocaleDateString([], { month: 'short', day: 'numeric' });
  }

  function _xScaleOptions(win, showTicks, gridColor, textColor) {
    return {
      type: 'linear', min: win.min, max: win.max,
      grid: { color: gridColor },
      ticks: { color: textColor, maxTicksLimit: 8, display: showTicks, callback: _xTick },
    };
  }

  function _baseOptions(win, opts) {
    return {
      responsive: true, maintainAspectRatio: false, animation: false,
      layout: { padding: { right: opts.padRight || 0, top: opts.padTop || 0 } },
      elements: { point: { hoverRadius: 0 } },
      interaction: { mode: 'index', intersect: false },
      plugins: {
        legend: { display: false },
        tooltip: { enabled: false },
        zoom: {
          zoom: {
            drag: { enabled: true, backgroundColor: 'rgba(33,150,243,0.12)' },
            pinch: { enabled: true },
            mode: 'x',
            onZoomComplete({ chart }) { _onZoomed(chart); },
          },
          pan: { enabled: false },
        },
      },
      scales: opts.scales,
    };
  }

  // Shared plugin: shaded future and the NOW line on both charts; event markers and the "Now" label on the indoor one.
  function _annotationPlugin(isIndoor, log) {
    return {
      id: 'caAnnotations',
      beforeDatasetsDraw(chart) {
        const xs = chart.scales.x, ys = chart.scales.y;
        if (!xs || !ys) return;
        const x0 = Math.max(xs.left, xs.getPixelForValue(Date.now()));
        if (x0 >= xs.right) return;
        const ctx = chart.ctx;
        ctx.save();
        ctx.fillStyle = 'rgba(128,128,128,0.07)';
        ctx.fillRect(x0, ys.top, xs.right - x0, ys.bottom - ys.top);
        ctx.restore();
      },
      afterDraw(chart) {
        const ctx = chart.ctx, xs = chart.scales.x, ys = chart.scales.y;
        if (!xs || !ys) return;
        const textColor = _cssVar('--text-secondary');
        const nowX = xs.getPixelForValue(Date.now());
        if (nowX >= xs.left && nowX <= xs.right) {
          ctx.save();
          ctx.strokeStyle = 'rgba(128,128,128,0.6)';
          ctx.lineWidth = 1.5;
          ctx.setLineDash([4, 4]);
          ctx.beginPath(); ctx.moveTo(nowX, ys.top); ctx.lineTo(nowX, ys.bottom); ctx.stroke();
          ctx.setLineDash([]);
          if (isIndoor) {
            ctx.fillStyle = textColor; ctx.font = '10px sans-serif'; ctx.textAlign = 'center';
            ctx.fillText('NOW', nowX, ys.top - 4);
          }
          ctx.restore();
        }
        if (isIndoor) _drawPlanMarkers(ctx, xs, ys);
        if (!isIndoor || !_stripOn('events')) return;
        log.forEach(e => {
          if (!e.event || !['override', 'classification_change'].includes(e.event)) return;
          const evtX = xs.getPixelForValue(_parseTs(e.ts));
          if (evtX < xs.left || evtX > xs.right) return;
          const color = e.event === 'override' ? '#f44336' : '#9e9e9e';
          ctx.save();
          ctx.strokeStyle = color; ctx.lineWidth = 1.5; ctx.setLineDash([4, 3]);
          ctx.beginPath(); ctx.moveTo(evtX, ys.top); ctx.lineTo(evtX, ys.bottom); ctx.stroke();
          ctx.setLineDash([]);
          ctx.fillStyle = color; ctx.font = '9px sans-serif'; ctx.textAlign = 'center';
          ctx.fillText(e.event === 'classification_change' ? 'Classify' : 'Override', evtX, ys.top + 10);
          ctx.restore();
        });
      },
    };
  }

  function _isolated(pts, i) {
    const p = pts && pts[i];
    if (!p || p.y == null) return false;
    const a = pts[i - 1], b = pts[i + 1];
    return (!a || a.y == null) && (!b || b.y == null);
  }

  function _dsFor(s) {
    const color = _cssVar('--c-' + s.color);
    return {
      caId: s.id,
      label: s.id.startsWith('v4:') ? 'v4 ' + s.label : s.id.startsWith('base:') ? 'No plan ' + s.label : s.label,
      type: 'line', data: s.data,
      borderColor: color, backgroundColor: 'transparent',
      borderWidth: s.width, borderDash: s.dash, borderCapStyle: s.dash.length && s.dash[0] <= 2 ? 'round' : 'butt',
      // a lone point between gaps (e.g. one daily mean in view) has no segment: draw it as a dot so it is visible
      pointRadius: c => _isolated(c.dataset.data, c.dataIndex) ? 2.5 : 0, pointStyle: 'circle', pointBackgroundColor: color,
      pointBorderWidth: 0, tension: 0.2, spanGaps: false, yAxisID: 'y',
      hidden: !_isOn(s.id) || !!s.disabledReason || s.raw.length === 0,
      order: s.kind === 'actual' ? 3 : 2,
    };
  }

  function drawChart(chartData) {
    if (!chartData) return;
    _lastChartData = chartData;
    _ensurePrefs();
    const canvas = document.getElementById('temp-chart');
    const outCanvas = document.getElementById('out-chart');
    if (!canvas || !outCanvas) return;
    if (_tempChart) { _tempChart.destroy(); _tempChart = null; }
    if (_outChart) { _outChart.destroy(); _outChart = null; }

    const S = _buildSeries(chartData);
    _series = S;
    _stripSegs = _stripSegments(chartData);
    _stripHasData = {};
    STRIP_ROWS.forEach(r => { _stripHasData[r.id] = (_stripSegs[r.id] || []).length > 0; });
    renderKey(chartData, S);

    const win = _viewWindow();
    const gridColor = _cssVar('--chart-grid');
    const textColor = _cssVar('--text-secondary');
    const log = chartData.state_log || [];
    const nowMs = Date.now();

    // ---- indoor chart datasets ----
    const datasets = S.filter(s => s.chart === 'in').map(_dsFor);

    // Target band (Issue #514: past from the immutable per-cycle snapshot, future from the live schedule)
    const bandOn = _isOn('ov:band');
    // Issue #1406: a row carrying hold_lower/hold_upper is drawn as that hold (a past time-of-use bank as what the
    // thermostat held, the same band the live chart drew); older rows and ordinary rows keep lower/upper.
    const _histHold = e => e.hold_lower != null && e.hold_upper != null;
    const histBandUpper = log.map(e => ({ x: _parseTs(e.ts), y: _histHold(e) ? e.hold_upper : e.upper })).filter(p => p.y != null && p.x <= nowMs);
    const histBandLower = log.map(e => ({ x: _parseTs(e.ts), y: _histHold(e) ? e.hold_lower : e.lower })).filter(p => p.y != null && p.x <= nowMs);
    const futBandUpper = (chartData.target_band || []).map(e => ({ x: _parseTs(e.ts), y: e.upper })).filter(p => p.x > nowMs);
    const futBandLower = (chartData.target_band || []).map(e => ({ x: _parseTs(e.ts), y: e.lower })).filter(p => p.x > nowMs);
    const bandUpper = [..._stepHeld(histBandUpper), ...futBandUpper].sort((a, b) => a.x - b.x);
    const bandLower = [..._stepHeld(histBandLower), ...futBandLower].sort((a, b) => a.x - b.x);
    if (bandUpper.length > 0 || bandLower.length > 0) {
      datasets.push({ caId: 'ov:band', label: 'Target Band', type: 'line', data: bandUpper, borderColor: _cssVar('--c-bandline'), borderWidth: 1,
        borderDash: [3, 3], backgroundColor: _cssVar('--c-band'), fill: '+1', pointRadius: 0, tension: 0, yAxisID: 'y', hidden: !bandOn, order: 5 });
      datasets.push({ caId: 'ov:band', label: '_target_band_lower', type: 'line', data: bandLower, borderColor: _cssVar('--c-bandline'), borderWidth: 1,
        borderDash: [3, 3], backgroundColor: 'transparent', fill: false, pointRadius: 0, tension: 0, yAxisID: 'y', hidden: !bandOn, order: 6 });
    }

    // Humidity / feels-like overlays
    const humidityPts = log.filter(e => e.humidity != null).map(e => ({ x: _parseTs(e.ts), y: e.humidity }));
    const feelsLikePts = log.filter(e => e.feelslike != null).map(e => ({ x: _parseTs(e.ts), y: e.feelslike }));
    datasets.push({ caId: 'ov:humidity', label: 'Humidity %', type: 'line', data: humidityPts, borderColor: _cssVar('--c-hum'), borderWidth: 1.5,
      pointRadius: 0, pointStyle: 'line', tension: 0.2, yAxisID: 'yRight', hidden: !_isOn('ov:humidity') || humidityPts.length === 0, order: 6 });
    datasets.push({ caId: 'ov:feelslike', label: 'Feels-like', type: 'line', data: feelsLikePts, borderColor: _cssVar('--c-feels'), borderWidth: 1.5,
      borderDash: [3, 3], pointRadius: 0, pointStyle: 'line', tension: 0.2, yAxisID: 'y', hidden: !_isOn('ov:feelslike') || feelsLikePts.length === 0, order: 7 });

    // The plan path (R3 timeline): where the house is predicted to go under the plan, from NOW on.
    const planPathPts = _planPath();
    if (planPathPts.length) {
      datasets.push({ caId: 'ov:plan', label: 'Plan path', type: 'line', data: planPathPts, borderColor: _cssVar('--c-plan'), borderWidth: 3,
        borderDash: [1, 5], borderCapStyle: 'round', pointRadius: 0, tension: 0.2, spanGaps: false, yAxisID: 'y', hidden: !_isOn('ov:plan'), order: 1 });
    }

    // The unified effective target line (Phase 3c): past from effective_target_history, future from the forecast;
    // null-target points stay in the series so a stretch with no target is a gap, never a bridged line (Issue #800).
    const histEffTarget = (chartData.effective_target_history || []).map(e => ({ x: _parseTs(e.ts), y: e.target })).filter(p => p.x <= nowMs);
    const futEffTarget = (chartData.effective_target_forecast || []).map(e => ({ x: _parseTs(e.ts), y: e.target })).filter(p => p.x > nowMs);
    const mergedEffTarget = [...histEffTarget, ...futEffTarget].sort((a, b) => a.x - b.x);
    const targetModeTimeline = [
      ...log.map(e => ({ x: _parseTs(e.ts), mode: _normalizeTargetMode(e.hvac) })),
      ...(chartData.predicted_activity || []).map(e => ({ x: _parseTs(e.ts), mode: _normalizeTargetMode(e.hvac_mode) })),
    ].sort((a, b) => a.x - b.x);
    function _targetModeAt(xMs) {
      let mode = null;
      for (const entry of targetModeTimeline) { if (entry.x > xMs) break; mode = entry.mode; }
      return mode;
    }
    const setpointVisible = _isOn('ov:target');
    datasets.push({
      caId: 'ov:target', label: 'Target', type: 'line', data: mergedEffTarget, borderColor: _cssVar('--c-target'),
      segment: { borderColor: (segCtx) => _targetModeColor(_targetModeAt(segCtx.p1.parsed.x)) },
      borderWidth: 4, pointRadius: 0, tension: 0, spanGaps: false, yAxisID: 'y',
      hidden: !setpointVisible || !mergedEffTarget.some(p => p.y != null), order: 8,
    });
    // Issue #1194 (d): while the remote-sensor outer loop holds an offset, "Target" is the room's comfort target and
    // this dashed line is what the thermostat was COMMANDED to (target + offset). Absent when no offset ever applied.
    const commandedPts = (chartData.commanded_setpoint_history || [])
      .map(e => ({ x: _parseTs(e.ts), y: e.target }))
      .filter(p => p.x <= nowMs);
    const _hasCommanded = commandedPts.some(p => p.y != null);
    const _cmdLegend = document.getElementById('legend-thermostat-set');
    if (_cmdLegend) _cmdLegend.style.display = _hasCommanded ? '' : 'none';
    if (_hasCommanded) {
      datasets.push({
        caId: 'ov:target', label: 'Thermostat set', type: 'line', data: commandedPts, borderColor: _cssVar('--c-target'),
        borderWidth: 2, borderDash: [6, 4], pointRadius: 0, tension: 0, spanGaps: false, yAxisID: 'y', hidden: !setpointVisible, order: 8,
      });
    }

    const humOn = _isOn('ov:humidity') && humidityPts.length > 0;
    const indoorScales = {
      x: _xScaleOptions(win, false, gridColor, textColor),
      y: { position: 'left', grid: { color: gridColor }, ticks: { color: textColor, callback: v => v + '°' }, afterFit(sc) { sc.width = OUT_PX; } },
      yRight: { position: 'right', display: true, grid: { display: false }, border: { display: false },
        ticks: { color: textColor, callback: v => (humOn ? v + '%' : '') }, afterFit(sc) { sc.width = RIGHT_PX; } },
    };
    try {
      _tempChart = new Chart(canvas, {
        type: 'line', data: { datasets },
        options: _baseOptions(win, { scales: indoorScales, padTop: 14 }),
        plugins: [_annotationPlugin(true, log)],
      });
    } catch (err) {
      console.error('Chart init failed:', err);
      _tempChart = null;
    }

    // ---- outdoor chart ----
    const outDatasets = S.filter(s => s.chart === 'out').map(_dsFor);
    const outScales = {
      x: _xScaleOptions(win, true, gridColor, textColor),
      y: { position: 'left', grid: { color: gridColor }, ticks: { color: textColor, maxTicksLimit: 4, callback: v => v + '°' }, afterFit(sc) { sc.width = OUT_PX; } },
    };
    try {
      _outChart = new Chart(outCanvas, {
        type: 'line', data: { datasets: outDatasets },
        options: _baseOptions(win, { scales: outScales, padRight: RIGHT_PX }),
        plugins: [_annotationPlugin(false, log)],
      });
    } catch (err) {
      console.error('Outdoor chart init failed:', err);
      _outChart = null;
    }

    const title = document.getElementById('chart-title');
    if (title) title.textContent = 'Temperature (' + _unitSym() + ')';
    const hasAnything = S.some(s => s.raw.length > 0);
    const empty = document.getElementById('chart-empty');
    if (empty) empty.hidden = hasAnything;
    drawStrips();
    renderKey(chartData, S);   // again, now that the datasets exist: a chip with nothing to draw in view is disabled
    _applyTableView();
    _refreshReadout();
  }

  // Both charts always show the same time span.
  function _applyWindow() {
    const win = _viewWindow();
    [_tempChart, _outChart].forEach(ch => {
      if (!ch) return;
      ch.options.scales.x.min = win.min;
      ch.options.scales.x.max = win.max;
      ch.update('none');
    });
    drawStrips();
    if (_lastChartData) renderKey(_lastChartData, _series);   // which chips can draw depends on the view
    _applyTableView();
    _refreshReadout();
  }

  function _onZoomed(chart) {
    const xs = chart.scales.x;
    _zoomWin = { min: xs.min, max: xs.max };
    _chartZoomed = true;
    const btn = document.getElementById('chart-zoom-reset');
    if (btn) btn.classList.add('visible');
    _applyWindow();
  }

  function resetChartZoom() {
    [_tempChart, _outChart].forEach(ch => { if (ch) { try { ch.resetZoom('none'); } catch (_e) { /* not zoomed */ } } });
    _zoomWin = null;
    _chartZoomed = false;
    const btn = document.getElementById('chart-zoom-reset');
    if (btn) btn.classList.remove('visible');
    _applyWindow();
  }

  // ---- activity strips ----
  function _hvacOf(e) { return (e.hvac || '').toLowerCase(); }

  // Issue #1436: solid = what happened (the chart log), faded = what the plan expects (the operation timeline's
  // `strips`, timeline_surfaces.strip_spans: window periods, nat-vent sessions, predicted runs, the setback and the
  // override the plan holds). Nothing right of NOW comes from chart_data.predicted_activity any more: that older
  // hour-by-hour predictor ignored the plan's close time and drew Nat-vent and Win plan all night.
  function _stripSegments(cd) {
    const log = (cd.state_log || []).slice().sort((a, b) => _parseTs(a.ts) - _parseTs(b.ts));
    const tests = {
      heating: e => _hvacOf(e) === 'heating',
      cooling: e => _hvacOf(e) === 'cooling',
      fan: e => e.fan_running === true || _hvacOf(e) === 'fan' || (e.fan_running === undefined && e.nat_vent_active === undefined && !!e.fan),
      natvent: e => !!e.nat_vent_active,
      windows: e => !!e.windows_open,
      // what the plan said at the time (windows_planned, #1436); older rows only have the classifier's live flag
      windowsplan: e => (typeof e.windows_planned === 'boolean' ? e.windows_planned : !!e.windows_recommended),
      away: e => e.occupancy === 'away' || e.occupancy === 'vacation',
      override: e => e.override === true,
    };
    const now = Date.now();
    const plan = (_timelineState === 'ok' && _timeline && Array.isArray(_timeline.strips)) ? _timeline.strips : [];
    const out = {};
    STRIP_ROWS.forEach(r => {
      const segs = [];
      const push = (a, b, future, step) => {
        if (!(b > a)) return;
        const last = segs[segs.length - 1];
        if (!future && last && !last.future && a <= last.b + 1) last.b = Math.max(last.b, b);
        else segs.push({ a, b, future, step: step || null });
      };
      // The past: a row's state holds from its time until the next row (rows are written at every change and every
      // 30 minutes, Issue #1425), never centred on the poll, and never beyond NOW (the last row holds up to 30 min).
      log.forEach((e, i) => {
        if (!tests[r.id](e)) return;
        const ts = _parseTs(e.ts);
        if (isNaN(ts) || ts >= now) return;
        const next = i < log.length - 1 ? _parseTs(log[i + 1].ts) : ts + 1800000;
        push(ts, Math.min(next, now), false);
      });
      // The plan, right of NOW: one segment per plan span, carrying the step its hover names.
      plan.forEach(sp => {
        if (!sp || sp.row !== r.id) return;
        const a = Math.max(_parseTs(sp.start), now), b = _parseTs(sp.end);
        if (isNaN(a) || isNaN(b)) return;
        // A run's bar is its expected minutes (owner decision); its hover covers the whole run window.
        const wa = sp.window_start ? _parseTs(sp.window_start) : NaN, wb = sp.window_end ? _parseTs(sp.window_end) : NaN;
        push(a, b, true, { text: sp.text || '', short: sp.text_short || sp.text || '', kind: sp.kind || '',
          at: sp.step_ts ? _parseTs(sp.step_ts) : null, actor: sp.actor || 'ca', confidence: sp.confidence || 'ok',
          winA: isNaN(wa) ? null : wa, winB: isNaN(wb) ? null : wb, minutes: sp.minutes == null ? null : sp.minutes });
      });
      out[r.id] = segs;
    });
    return out;
  }

  // The plan span a faded strip draws at `ms` for each strip that is on (Issue #1436: the hover names the plan step).
  function _stripPlanAt(ms) {
    if (!(ms > Date.now())) return [];
    const found = [];
    STRIP_ROWS.forEach(r => {
      if (!_stripOn(r.id)) return;
      const segs = (_stripSegs[r.id] || []).filter(g => g.future);
      const sg = segs.find(g => g.a <= ms && ms < g.b) ||
        segs.find(g => g.step && g.step.winA != null && g.step.winA <= ms && ms < g.step.winB);
      if (sg && sg.step) found.push({ row: r, step: sg.step });
    });
    return found;
  }

  // Issue #1436: one line under the strips says what solid and faded mean (both dashboards: the module adds it).
  const STRIP_LEGEND_TEXT = 'Solid: what happened \u00b7 Faded: what the plan expects';
  function _stripLegend(wrap, rows) {
    let el = document.getElementById('chart-strip-legend');
    if (!el) {
      el = document.createElement('p');
      el.id = 'chart-strip-legend';
      el.className = 'strip-legend';
      el.innerHTML = '<span class="strip-legend-sw strip-legend-solid" aria-hidden="true"></span>Solid: what happened' +
        ' \u00b7 <span class="strip-legend-sw strip-legend-faded" aria-hidden="true"></span>Faded: what the plan expects';
      el.setAttribute('aria-label', STRIP_LEGEND_TEXT);
      wrap.appendChild(el);
    }
    el.hidden = !rows.length;
  }

  function drawStrips() {
    const canvas = document.getElementById('activity-chart');
    if (!canvas) return;
    const rows = STRIP_ROWS.filter(r => _stripOn(r.id) && (_stripSegs[r.id] || []).length > 0);
    const wrap = canvas.parentElement;
    wrap.style.display = rows.length ? '' : 'none';
    _stripLegend(wrap, rows);
    window.__test_strips = { rows: rows.map(r => r.id), segs: _stripSegs };   // test hook (tests/ui/strips-plan.spec.js)
    if (!rows.length) return;
    const xs = _tempChart && _tempChart.scales && _tempChart.scales.x ? _tempChart.scales.x : null;
    const win = _viewWindow();
    const rangeMin = xs ? xs.min : win.min, rangeMax = xs ? xs.max : win.max;
    const dpr = window.devicePixelRatio || 1;
    const W = wrap.clientWidth;
    const rowH = 12, gap = 3;
    const H = rows.length * rowH + (rows.length - 1) * gap;
    canvas.style.width = W + 'px';
    canvas.style.height = H + 'px';
    canvas.width = W * dpr;
    canvas.height = H * dpr;
    const ctx = canvas.getContext('2d');
    ctx.scale(dpr, dpr);
    const plotLeft = xs ? xs.left : OUT_PX;
    const plotRight = xs ? xs.right : W - RIGHT_PX;
    const plotW = plotRight - plotLeft;
    const span = rangeMax - rangeMin;
    const xPx = ms => plotLeft + ((ms - rangeMin) / span) * plotW;
    const labelColor = _cssVar('--text-secondary');
    ctx.clearRect(0, 0, W, H);
    ctx.font = '9px sans-serif';
    ctx.textBaseline = 'middle';
    rows.forEach((r, i) => {
      const y = i * (rowH + gap);
      ctx.fillStyle = _cssVar('--chart-track');
      ctx.fillRect(plotLeft, y, plotW, rowH);
      ctx.fillStyle = r.color;
      (_stripSegs[r.id] || []).forEach(sg => {
        const a = Math.max(rangeMin, sg.a), b = Math.min(rangeMax, sg.b);
        if (b <= rangeMin || a >= rangeMax) return;
        ctx.globalAlpha = sg.future ? 0.4 : 1;
        ctx.fillRect(xPx(a), y, Math.max(1, xPx(b) - xPx(a)), rowH);
      });
      ctx.globalAlpha = 1;
      ctx.fillStyle = labelColor;
      ctx.textAlign = 'right';
      ctx.fillText(r.short, plotLeft - 4, y + rowH / 2);
    });
    const nowX = xPx(Date.now());
    if (nowX >= plotLeft && nowX <= plotRight) {
      ctx.strokeStyle = 'rgba(128,128,128,0.6)';
      ctx.lineWidth = 1;
      ctx.setLineDash([3, 3]);
      ctx.beginPath(); ctx.moveTo(nowX, 0); ctx.lineTo(nowX, H); ctx.stroke();
      ctx.setLineDash([]);
    }
  }

  // ---- hover readout and the touch pin ----
  function _setCursor(ms, pinned) {
    _cursorMs = ms;
    _cursorPinned = !!pinned;
    const line = document.getElementById('chart-cursor');
    const xs = _tempChart && _tempChart.scales ? _tempChart.scales.x : null;
    if (line && xs) {
      const px = xs.getPixelForValue(ms);
      if (px >= xs.left && px <= xs.right) {
        line.hidden = false;
        line.style.left = px + 'px';
        line.classList.toggle('pinned', !!pinned);
        window.__test_cursorPx = px;
      } else {
        line.hidden = true;
      }
    }
    _renderReadout(ms);
  }

  // The pointer left the chart (or a pin was released, or Esc): the readout goes back to resting on NOW. Its height is
  // kept (the tallest it has reached), so entering and leaving the chart never makes the content below it jump.
  function _clearCursor() {
    _cursorMs = null;
    _cursorPinned = false;
    window.__test_cursorPx = null;
    const line = document.getElementById('chart-cursor');
    if (line) line.hidden = true;
    _renderRest();
  }

  // Re-render whatever the readout shows (a hover/pin, or NOW) after the chart data, the window or the lines changed.
  function _refreshReadout() {
    if (_cursorMs != null) _setCursor(_cursorMs, _cursorPinned);
    else _renderRest();
  }

  // Issue #1408: with the pointer off the chart the readout is not blank; it shows what a hover at NOW would. When NOW
  // is outside the visible range (scrolled into history, or zoomed elsewhere) it shows the right edge of the view.
  function _restPoint() {
    const now = Date.now();
    const win = _viewWindow();
    if (now >= win.min && now <= win.max) return { ms: now, label: 'Now \u00b7 ' + _clock(now) };
    const ms = win.max;
    const day = Math.abs(ms - now) < 6 * 86400000
      ? new Date(ms).toLocaleDateString([], { weekday: 'short' })
      : new Date(ms).toLocaleDateString([], { month: 'short', day: 'numeric' }) + ',';
    return { ms: ms, label: day + ' ' + _clock(ms) + ' (end of view)' };
  }

  function _renderRest() {
    const r = _restPoint();
    _renderReadout(r.ms, r.label);
  }

  function _releaseReadoutHeight() {
    _readoutHeld = 0;
    _readoutFresh = true;
    const panel = document.getElementById('chart-hover-panel');
    if (panel) panel.style.minHeight = '';
  }

  // Hold the readout at the tallest height it has reached, so moving between points with fewer rows, or between a
  // hover and NOW, does not make the content below the chart jump up and down. Released when the lines shown change
  // (a key toggle) or the readout's width changes, then re-held at the new content's height. prevH is the height of
  // what was on screen just before this render (it may have been rendered while the panel could not be measured).
  function _holdReadoutHeight(panel, prevH) {
    const h = Math.max(panel.offsetHeight, prevH || 0);
    _readoutHeldW = panel.offsetWidth;
    if (h > _readoutHeld) { _readoutHeld = h; panel.style.minHeight = h + 'px'; }
  }

  function _chpItem(color, text) {
    return '<div class="chp-item"><span class="chp-dot" style="background:' + color + '"></span><span>' + text + '</span></div>';
  }

  function _readoutRow(label, items) {
    return items ? '<div class="chp-row"><span class="chp-row-label">' + label + '</span>' + items + '</div>' : '';
  }

  // restLabel: the readout is resting (pointer off the chart, nothing pinned) at NOW or the view's right edge; the
  // label replaces the hovered time and a short hint keeps hover and pin discoverable. Same rows as a hover.
  function _renderReadout(ms, restLabel) {
    const panel = document.getElementById('chart-hover-panel');
    if (!panel) return;
    const sym = '°';
    const isPast = ms <= Date.now();
    const lines = { indoor: '', predicted: '', outdoor: '' };
    _series.forEach(s => {
      if (!_isOn(s.id) || s.disabledReason || s.raw.length === 0) return;
      const v = _valueAt(s, ms);
      if (v == null) return;
      let miss = '';
      if (isPast && s.missVs) {
        const ref = _seriesById(s.missVs);
        const a = ref ? _valueAt(ref, ms) : null;
        if (a != null) miss = ' <span class="chp-miss" title="' + escapeHtml(s.short) + ' minus what was read">(' + _fmtSigned(v - a) + ')</span>';
      }
      const item = _chpItem('var(--c-' + s.color + ')', escapeHtml(s.short) + ': <strong>' + _fmt(v, 1) + sym + '</strong>' + miss);
      lines[s.group] += item;
    });
    const log = (_lastChartData && _lastChartData.state_log) || [];
    // Overlay values (humidity, feels-like, target) come from their chart datasets.
    let overlay = '';
    if (_tempChart) {
      _tempChart.data.datasets.forEach(ds => {
        if (ds.hidden || !ds.data || !ds.data.length || ds.label.startsWith('_') || ds.label === 'Target Band') return;
        if (!['Humidity %', 'Feels-like', 'Target', 'Thermostat set'].includes(ds.label)) return;
        const s = { data: ds.data, tol: 90 * 60000 };
        const v = _valueAt(s, ms);
        if (v == null) return;
        const col = ds.label === 'Humidity %' ? 'var(--c-hum)' : ds.label === 'Feels-like' ? 'var(--c-feels)' : 'var(--c-target)';
        overlay += _chpItem(col, escapeHtml(ds.label === 'Humidity %' ? 'Humidity' : ds.label) + ': <strong>' + Math.round(v) + (ds.label === 'Humidity %' ? '%' : sym) + '</strong>');
      });
    }
    if (_tempChart && _isOn('ov:band')) {
      const up = _tempChart.data.datasets.find(d => d.label === 'Target Band');
      const lo = _tempChart.data.datasets.find(d => d.label === '_target_band_lower');
      const hi = up ? _valueAt({ data: up.data, tol: 90 * 60000 }, ms) : null;
      const lw = lo ? _valueAt({ data: lo.data, tol: 90 * 60000 }, ms) : null;
      if (hi != null && lw != null) {
        const who = _bandLabelAt(ms);
        overlay += _chpItem('var(--c-bandline)', 'Band' + (who ? ' (' + escapeHtml(who) + ')' : '') + ': <strong>' + Math.round(lw) + '\u2013' + Math.round(hi) + sym + '</strong>');
      }
    }
    const planStep = ms >= Date.now() - 60000 ? _planNear(ms) : null;   // NOW itself (a minute's slack) counts as ahead
    if (planStep) overlay += _chpItem(planStep.actor === 'you' ? 'var(--c-target)' : 'var(--c-plan)',
      escapeHtml(planStep.actor === 'you' ? 'You' : 'CA') + ' ' + escapeHtml(_clock(planStep._ms)) + ': <strong>' + escapeHtml(planStep.text_short || planStep.text || '') + '</strong>');
    // Issue #1436: right of NOW each faded strip names the plan step it draws ("Win plan: 7:14 PM: close the windows ...")
    let activity = '';
    // (the step's time and who acts beside it, its sentence without the time, as the plan overlay's line does, #1399)
    _stripPlanAt(ms).forEach(({ row, step }) => {
      const who = step.at == null ? '' : ' \u00b7 ' + (step.actor === 'you' ? 'You' : 'CA') + ' ' + escapeHtml(_clock(step.at));
      activity += _chpItem(row.color, escapeHtml(row.short) + ' <span class="chp-plan-tag">plan</span>' + who + ': <strong>' +
        escapeHtml(step.short) + '</strong>' + (step.confidence === 'low' ? ' <span class="plan-qual">may help</span>' : ''));
    });
    let closest = null, minDiff = Infinity;
    log.forEach(e => {
      const diff = Math.abs(_parseTs(e.ts) - ms);
      if (diff < minDiff) { minDiff = diff; closest = e; }
    });
    let stateHtml = '';
    if (closest && minDiff < 3 * 3600000) {
      const hvacColors = { heating: '#ef5350', cooling: '#42a5f5', fan: '#66bb6a' };
      const hvacText = { heating: 'Heating', cooling: 'Cooling', fan: 'Fan-Only' };
      const hv = _hvacOf(closest);
      if (hvacColors[hv]) stateHtml += '<div class="chp-item"><span class="chp-state" style="background:' + hvacColors[hv] + '">' + hvacText[hv] + '</span></div>';
      if (closest.fan && hv !== 'fan') stateHtml += '<div class="chp-item"><span class="chp-state" style="background:#66bb6a">Sched fan</span></div>';
      if (closest.event) stateHtml += '<div class="chp-item" style="color:var(--text-secondary);font-size:0.73rem">' + escapeHtml(String(closest.event).replace(/_/g, ' ')) + '</div>';
    }
    const when = restLabel ? escapeHtml(restLabel)
      : new Date(ms).toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
    const touchOnly = window.matchMedia && window.matchMedia('(hover: none)').matches;
    const pin = restLabel
      ? '<span class="chp-hint">' + (touchOnly ? 'Tap the chart to pin a time' : 'Hover the chart for any time; click to pin') + '</span>'
      : _cursorPinned ? '<button type="button" class="chp-clear" onclick="clearChartPin()">Pinned · ' + (touchOnly ? 'tap' : 'click') + ' to release</button>' : '';
    const body = _readoutRow('Indoor', lines.indoor) + _readoutRow('Predicted', lines.predicted) + _readoutRow('Outdoor', lines.outdoor) +
      _readoutRow('Overlays', overlay) + _readoutRow('Activity', activity) + (stateHtml ? '<div class="chp-row">' + stateHtml + '</div>' : '');
    const prevH = _readoutFresh ? 0 : panel.offsetHeight;
    _readoutFresh = false;
    panel.dataset.mode = restLabel ? 'rest' : (_cursorPinned ? 'pinned' : 'hover');
    // Resting on a moment with nothing to read (a stretch with no data in view) still says which moment it is.
    const grid = body || (restLabel && _lastChartData ? '<div class="chp-row chp-none">No readings at this time.</div>' : '');
    panel.innerHTML = grid ? '<div class="chp-time">' + when + pin + '</div><div class="chp-grid">' + grid + '</div>' : '';
    _holdReadoutHeight(panel, prevH);
  }

  function clearChartPin() { _clearCursor(); }

  function _msAtClientX(clientX) {
    const canvas = document.getElementById('temp-chart');
    const xs = _tempChart && _tempChart.scales ? _tempChart.scales.x : null;
    if (!canvas || !xs) return null;
    const r = canvas.getBoundingClientRect();
    const px = clientX - r.left;
    window.__test_cursorPx = px;
    if (px < xs.left || px > xs.right) return null;
    return xs.getValueForPixel(px);
  }

  // Test hook: simulate a hover at canvas-relative x pixel (used by the browser tests)
  window.__triggerHoverAt = function(canvasX) {
    const xs = _tempChart && _tempChart.scales ? _tempChart.scales.x : null;
    if (!xs) return;
    _setCursor(xs.getValueForPixel(canvasX), false);
  };

  function _initChartInteraction() {
    const stack = document.getElementById('chart-stack');
    if (!stack || stack.dataset.wired === '1') return;
    stack.dataset.wired = '1';
    stack.addEventListener('mousemove', e => {
      if (Date.now() - _lastTouchAt < 700 || _cursorPinned) return;   // a synthesised mouse event after a tap
      const ms = _msAtClientX(e.clientX);
      if (ms == null) { if (_cursorMs != null) _clearCursor(); return; }   // an axis gutter: back to NOW
      _setCursor(ms, false);
    });
    stack.addEventListener('mouseleave', () => { if (!_cursorPinned) _clearCursor(); });
    ['temp-chart', 'out-chart', 'activity-chart'].forEach(id => {
      const c = document.getElementById(id);
      if (c) c.addEventListener('mouseleave', () => { if (!_cursorPinned && _cursorMs != null) _clearCursor(); });
    });
    // Desktop pin: a click freezes the readout on that moment; the next click (or Esc) lets go. A drag (zoom select)
    // is not a click, and the mouse events a browser synthesises after a tap are left to the touch pin below.
    stack.addEventListener('mousedown', e => { _downX = e.clientX; _downY = e.clientY; });
    stack.addEventListener('click', e => {
      if (Date.now() - _lastTouchAt < 700) return;
      const dragged = _downX != null && (Math.abs(e.clientX - _downX) > 5 || Math.abs(e.clientY - _downY) > 5);
      _downX = _downY = null;
      if (dragged) return;
      if (_cursorPinned) { _clearCursor(); const ms = _msAtClientX(e.clientX); if (ms != null) _setCursor(ms, false); return; }
      const ms = _msAtClientX(e.clientX);
      if (ms != null) _setCursor(ms, true);
    });
    stack.addEventListener('touchstart', e => {
      _multiTouch = e.touches.length > 1;
      if (e.touches.length === 1) {
        _touchStartX = e.touches[0].clientX;
        _touchStartY = e.touches[0].clientY;
        _touchStartAt = Date.now();
      }
    }, { passive: true });
    stack.addEventListener('touchend', e => {
      _lastTouchAt = Date.now();
      if (_multiTouch) { if (e.touches.length === 0) _multiTouch = false; return; }
      const t = e.changedTouches[0];
      const dx = t.clientX - _touchStartX, dy = t.clientY - _touchStartY;
      if (Math.abs(dx) <= 10 && Math.abs(dy) <= 10 && Date.now() - _touchStartAt < 600) {
        // tap: pin the readout at this time; tapping the same spot again lets go
        const ms = _msAtClientX(t.clientX);
        if (ms == null) return;
        const xs = _tempChart.scales.x;
        if (_cursorPinned && _cursorMs != null && Math.abs(xs.getPixelForValue(_cursorMs) - xs.getPixelForValue(ms)) < 14) _clearCursor();
        else _setCursor(ms, true);
      } else if (Math.abs(dx) > 30 && Math.abs(dx) > Math.abs(dy) * 1.5) {
        shiftChartWindow(dx > 0 ? -1 : 1);
      }
    }, { passive: true });
    stack.addEventListener('keydown', e => {
      const xs = _tempChart && _tempChart.scales ? _tempChart.scales.x : null;
      if (!xs) return;
      if (e.key === 'Escape') { _clearCursor(); return; }
      if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') return;
      e.preventDefault();
      const step = (xs.max - xs.min) / 60;
      const from = _cursorMs != null ? _cursorMs : Date.now();
      _setCursor(Math.min(xs.max, Math.max(xs.min, from + (e.key === 'ArrowRight' ? step : -step))), true);
    });
    const key = document.getElementById('chart-key');
    if (key) {
      key.addEventListener('click', e => {
        const chip = e.target.closest('[data-series]');
        if (chip && !chip.disabled) { toggleSeries(chip.dataset.series); return; }
        const lead = e.target.closest('[data-lead]');
        if (lead && !lead.disabled) setChartLead(lead.dataset.lead);
      });
      key.addEventListener('toggle', _onKeyToggle);
    }
    window.addEventListener('resize', () => {
      clearTimeout(_initChartInteraction._t);
      _initChartInteraction._t = setTimeout(() => {
        _syncKeyOpen();
        // Only a width change re-flows the readout's grid; a height-only resize (a phone's address bar) keeps the hold.
        const hp = document.getElementById('chart-hover-panel');
        if (hp && hp.offsetWidth !== _readoutHeldW) _releaseReadoutHeight();
        if (_lastChartData) drawChart(_lastChartData);
      }, 150);
    });
    if (window.matchMedia) {
      const mq = window.matchMedia('(prefers-color-scheme: dark)');
      if (mq.addEventListener) mq.addEventListener('change', () => { if (_lastChartData) drawChart(_lastChartData); });
    }
    _syncKeyOpen();
  }

  // The key folds into a one-line "Showing: ..." summary at every width: folded on a first visit, then open or folded
  // as the person left it (remembered with the line choices). It sits above the chart's box, so the chart keeps its size.
  function _syncKeyOpen() {
    const key = document.getElementById('chart-key');
    if (!key) return;
    const open = !!(_chartPrefs && _chartPrefs.keyOpen);
    if (key.open !== open) key.open = open;
    _renderKeySummary();
  }
  function _onKeyToggle() {
    const key = document.getElementById('chart-key');
    _renderKeySummary();
    if (!key) return;
    _ensurePrefs();
    if (_chartPrefs.keyOpen === key.open) return;
    _chartPrefs.keyOpen = key.open;
    _writePrefs();
  }

  // ---- toggles and controls ----
  function toggleSeries(id) {
    _ensurePrefs();
    _chartPrefs.on[_prefId(id)] = !_isOn(id);
    _writePrefs();
    if (id.startsWith('strip:')) {
      drawStrips();
      if (id === 'strip:events' && _tempChart) _tempChart.draw();
      renderKey(_lastChartData, _series);
      return;
    }
    // Keep the chart objects: flip the dataset(s) and let the axes re-fit.
    const on = _isOn(id);
    [_tempChart, _outChart].forEach(ch => {
      if (!ch) return;
      ch.data.datasets.forEach(d => {
        if (d.caId !== id) return;
        const s = _seriesById(id);
        const hasData = s ? (s.raw.length > 0 && !s.disabledReason) : (d.data && d.data.length > 0);
        d.hidden = !on || !hasData;
      });
      ch.update('none');
    });
    renderKey(_lastChartData, _series);
    _applyTableView();
    _releaseReadoutHeight();   // the rows changed: size to the new content, then hold that
    _refreshReadout();
  }

  function toggleOverlay(name) {
    const map = { comfort_band: 'ov:band', setpoint: 'ov:target', plan: 'ov:plan' };   // humidity / feels-like: Issue #1414
    if (map[name]) toggleSeries(map[name]);
  }

  function _syncRangeButtons() {
    document.querySelectorAll('.range-btn').forEach(b => {
      const on = b.dataset.range === _chartRange;
      b.classList.toggle('active', on);
      b.setAttribute('aria-pressed', on ? 'true' : 'false');
    });
    document.querySelectorAll('.ahead-btn').forEach(b => {
      const on = b.dataset.ahead === _chartAhead;
      b.classList.toggle('active', on);
      b.setAttribute('aria-pressed', on ? 'true' : 'false');
    });
  }

  function setChartRange(range, btn) {
    _ensurePrefs();
    _chartRange = range;
    _chartPrefs.range = range;
    _writePrefs();
    _windowOffset = 0;
    _zoomWin = null;
    _chartZoomed = false;
    _syncRangeButtons();
    const resetBtn = document.getElementById('chart-zoom-reset');
    if (resetBtn) resetBtn.classList.remove('visible');
    loadChart();
  }

  function setChartAhead(ahead) {
    if (!CHART_AHEADS.includes(ahead)) return;
    _ensurePrefs();
    _chartAhead = ahead;
    _chartPrefs.ahead = ahead;
    _writePrefs();
    _zoomWin = null;
    _chartZoomed = false;
    _syncRangeButtons();
    const resetBtn = document.getElementById('chart-zoom-reset');
    if (resetBtn) resetBtn.classList.remove('visible');
    if (_isHistoricalView) loadChart(); else if (_lastChartData) drawChart(_lastChartData);
  }

  function setChartLead(lead) {
    if (!CHART_LEADS.includes(lead)) return;
    _ensurePrefs();
    _chartLead = lead;
    _chartPrefs.lead = lead;
    _writePrefs();
    if (_lastChartData) drawChart(_lastChartData);
  }

  // ---- table view (what the chart shows, as an accessible table) ----
  function toggleTableView() {
    _ensurePrefs();
    _tableView = !_tableView;
    _chartPrefs.table = _tableView;
    _writePrefs();
    _syncTableToggle();
    _applyTableView();
  }

  function _syncTableToggle() {
    const btn = document.getElementById('chart-table-toggle');
    if (btn) { btn.setAttribute('aria-pressed', _tableView ? 'true' : 'false'); btn.classList.toggle('active', _tableView); }
  }

  function _tableStep(spanMs) {
    const h = spanMs / 3600000;
    if (h <= 36) return 1800000;
    if (h <= 96) return 3600000;
    if (h <= 384) return 3 * 3600000;
    if (h <= 2400) return 12 * 3600000;
    return 86400000;
  }

  // The visible window as rows: one row per step, one column per visible line (nothing hidden is exported).
  function buildTableModel() {
    const win = _viewWindow();
    const step = _tableStep(win.max - win.min);
    const cols = _series.filter(s => _isOn(s.id) && !s.disabledReason && s.raw.length > 0);
    const rows = [];
    const first = Math.ceil(win.min / step) * step;
    const log = ((_lastChartData && _lastChartData.state_log) || []);
    for (let t = first; t <= win.max; t += step) {
      let state = '';
      let best = null, bestD = step;
      for (const e of log) { const d = Math.abs(_parseTs(e.ts) - t); if (d < bestD) { bestD = d; best = e; } }
      if (best) state = _hvacOf(best);
      rows.push({ t, cells: cols.map(s => _valueAt(s, t)), state });
    }
    return { cols, rows, step };
  }

  function _applyTableView() {
    const wrap = document.getElementById('chart-table-wrap');
    const stack = document.getElementById('chart-stack');
    if (!wrap || !stack) return;
    wrap.hidden = !_tableView;
    stack.style.display = _tableView ? 'none' : '';
    const hover = document.getElementById('chart-hover-panel');
    if (hover) hover.style.display = _tableView ? 'none' : '';
    if (!_tableView || !_lastChartData) return;
    const m = buildTableModel();
    const sym = _unitSym();
    const fmtT = t => new Date(t).toLocaleString([], { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' });
    const head = '<tr><th scope="col">Time</th>' + m.cols.map(c => '<th scope="col">' + escapeHtml(c.short) + ' (' + sym + ')</th>').join('') + '<th scope="col">Thermostat</th></tr>';
    const body = m.rows.map(r => '<tr' + (r.t > Date.now() ? ' class="future"' : '') + '><th scope="row">' + escapeHtml(fmtT(r.t)) + '</th>' +
      r.cells.map(v => '<td>' + (v == null ? '—' : v.toFixed(1)) + '</td>').join('') + '<td>' + escapeHtml(r.state || '') + '</td></tr>').join('');
    wrap.querySelector('table').innerHTML = '<caption>Temperatures in ' + escapeHtml(sym) + ', every ' + Math.round(m.step / 60000) +
      ' minutes. Rows after now are predictions.</caption><thead>' + head + '</thead><tbody>' + body + '</tbody>';
  }

  function copyChartTable() {
    const m = buildTableModel();
    const sym = _unitSym();
    const lines = [['Time'].concat(m.cols.map(c => c.short + ' (' + sym + ')'), ['Thermostat']).join('\t')];
    m.rows.forEach(r => lines.push([new Date(r.t).toISOString()].concat(r.cells.map(v => v == null ? '' : v.toFixed(1)), [r.state || '']).join('\t')));
    _copyText(lines.join('\n'));
    const btn = document.getElementById('chart-table-copy');
    if (btn) { btn.textContent = 'Copied'; setTimeout(() => { btn.textContent = 'Copy table'; }, 1500); }
  }

  window.__chartState = function() {
    return {
      range: _chartRange, ahead: _chartAhead, lead: _chartLead, zoomed: _chartZoomed, zoomWin: _zoomWin, table: _tableView,
      offset: _windowOffset, cursor: _cursorMs, pinned: _cursorPinned, prefs: _chartPrefs,
      series: _series.map(s => ({ id: s.id, n: s.raw.length, on: _isOn(s.id), disabled: s.disabledReason })),
      win: _viewWindow(),
    };
  };


  async function loadChart(beforeTs = null) {
    _ensurePrefs();
    try {
      // A scrolled (historical) fetch must cover the whole window, past and ahead; a live one needs only the past.
      const fetchRange = beforeTs ? _coveringRange(_hoursOf(_chartRange) + _hoursOf(_chartAhead)) : _chartRange;
      let url = `chart_data?range=${fetchRange}`;
      if (beforeTs) url += `&before_ts=${Math.round(beforeTs)}`;
      const data = await _host.api(url);
      _dataWindowStart = _getDataMinTs(data);
      _dataWindowEnd   = _getDataMaxTs(data);
      _isHistoricalView = (beforeTs !== null);
      _lastChartData   = data;
      drawChart(data);
      _host.onData(data);
    } catch (e) {
      console.error('Chart load failed:', e);
    }
  }

  // ---- plan: the operation timeline (R3) as an overlay and as the "Next 24 hours" list ----
  //
  // Occupant view: the chart's right-hand side shows what Climate Advisor will do (round markers) and what the
  // occupant is asked to do (diamond markers; hollow when the step only may help), and the list under it says the
  // same in words. Both read GET /api/climate_advisor/timeline; nothing here recomputes the plan.
  const PLAN_HORIZON_MS = 24 * 3600000;
  const PLAN_HIDE_KINDS = ['briefing_send'];   // the daily briefing send is how the occupant is reading this
  let _timeline = null;            // last /timeline payload, or null
  let _timelineState = 'idle';     // idle | ok | none (no plan yet, HTTP 503) | error
  let _planMounts = [];            // [{ el, extra }]

  function _planSteps(horizonMs) {
    if (!_timeline || !Array.isArray(_timeline.steps)) return [];
    const now = Date.now();
    return _timeline.steps
      .map(s => Object.assign({}, s, { _ms: _parseTs(s.ts) }))
      .filter(s => !isNaN(s._ms) && s._ms > now - 10 * 60000 && s._ms <= now + horizonMs &&
        !PLAN_HIDE_KINDS.includes(s.kind) && !(s.meta && s.meta.status === 'done'))
      .sort((a, b) => a._ms - b._ms);
  }

  // The plan path for the chart: the hallway/CA line of the timeline from NOW on. A v4 path is drawn only when v4 has
  // data (the same availability as the v4 lines).
  function _planPath() {
    if (!_timeline || !Array.isArray(_timeline.path) || !_timeline.path.length) return [];
    const v4ok = !!(_lastChartData && _lastChartData.v4 && _lastChartData.v4.ready === true);
    const pts = _timeline.path.filter(p => p && p.temp_f != null && (p.model !== 'v4' || v4ok));
    if (!pts.length) return [];
    const sensors = [...new Set(pts.map(p => p.sensor || '_ca'))];
    const pick = sensors.includes('_ca') ? '_ca' : sensors[0];
    const now = Date.now();
    // Both payloads are in the display unit; if they ever disagree (a stale cache across a unit change) follow the chart.
    const chartUnit = _lastChartData && _lastChartData.unit, tlUnit = _timeline.unit;
    const conv = (chartUnit && tlUnit && chartUnit !== tlUnit)
      ? (chartUnit === 'celsius' ? v => (v - 32) * 5 / 9 : v => v * 9 / 5 + 32) : v => v;
    return pts.filter(p => (p.sensor || '_ca') === pick).map(p => ({ x: _parseTs(p.ts), y: conv(p.temp_f) }))
      .filter(p => !isNaN(p.x) && p.x >= now - 30 * 60000).sort((a, b) => a.x - b.x);
  }

  function _planHas() { return _planSteps(5 * 86400000).length > 0 || _planPath().length > 0; }

  function _drawPlanMarkers(ctx, xs, ys) {
    if (!_isOn('ov:plan')) return;
    const steps = _planSteps(5 * 86400000);
    if (!steps.length) return;
    const now = Date.now();
    const caColor = _cssVar('--c-plan'), youColor = _cssVar('--c-target'), ink = _cssVar('--c-ink');
    steps.forEach(s => {
      if (s._ms <= now) return;
      const x = xs.getPixelForValue(s._ms);
      if (x < xs.left || x > xs.right) return;
      const you = s.actor === 'you', low = s.confidence === 'low';
      ctx.save();
      ctx.strokeStyle = ink; ctx.globalAlpha = 0.18; ctx.lineWidth = 1; ctx.setLineDash([2, 3]);
      ctx.beginPath(); ctx.moveTo(x, ys.top + 12); ctx.lineTo(x, ys.bottom); ctx.stroke();
      ctx.globalAlpha = 1; ctx.setLineDash([]);
      const y = ys.top + 8;
      ctx.lineWidth = 1.5; ctx.strokeStyle = you ? ink : caColor; ctx.fillStyle = you ? youColor : caColor;
      ctx.beginPath();
      if (you) { ctx.moveTo(x, y - 6); ctx.lineTo(x + 6, y); ctx.lineTo(x, y + 6); ctx.lineTo(x - 6, y); ctx.closePath(); }
      else ctx.arc(x, y, 4.5, 0, Math.PI * 2);
      if (low) { ctx.fillStyle = _cssVar('--card-bg'); }
      ctx.fill(); ctx.stroke();
      ctx.restore();
    });
  }

  function _planNear(ms) {
    if (!_isOn('ov:plan')) return null;
    let best = null, bd = 20 * 60000 + 1;
    _planSteps(5 * 86400000).forEach(s => { const d = Math.abs(s._ms - ms); if (d < bd) { bd = d; best = s; } });
    return best;
  }

  function _clock(ms) { return new Date(ms).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' }); }

  function _planRowHtml(s, now) {
    const you = s.actor === 'you', low = s.confidence === 'low';
    const held = !!(s.meta && s.meta.status === 'held');   // a held step (a hold or the live direction veto) is not a "may help" offer
    const sameDay = new Date(s._ms).toDateString() === new Date(now).toDateString();
    const day = sameDay ? '' : '<span class="plan-day">' + escapeHtml(new Date(s._ms).toLocaleDateString([], { weekday: 'short' })) + '</span> ';
    const lead = you && s.lead_minutes > 0 && s._ms - s.lead_minutes * 60000 > now
      ? '<span class="plan-note">Reminder at ' + escapeHtml(_clock(s._ms - s.lead_minutes * 60000)) + '</span>' : '';
    return '<li class="plan-row ' + (you ? 'plan-you' : 'plan-ca') + (low ? ' plan-low' : '') + (held ? ' plan-held' : '') + '" data-kind="' + escapeHtml(s.kind || '') + '" data-actor="' + escapeHtml(s.actor || '') + '">' +
      '<span class="plan-time">' + day + escapeHtml(_clock(s._ms)) + '</span>' +
      '<span class="plan-actor" title="' + (you ? 'You do this' : 'Climate Advisor does this') + '">' +
        '<svg width="12" height="12" viewBox="-7 -7 14 14" aria-hidden="true">' + (you
          ? '<path d="M0 -6 L6 0 L0 6 L-6 0 Z" fill="' + (low ? 'none' : 'var(--c-target)') + '" stroke="var(--c-ink)" stroke-width="1.5"/>'
          : '<circle r="4.5" fill="' + (low ? 'none' : 'var(--c-plan)') + '" stroke="var(--c-plan)" stroke-width="1.5"/>') + '</svg>' +
        (you ? 'You' : 'CA') + '</span>' +
      '<span class="plan-text">' + escapeHtml(s.text_short || s.text || '') + (low && !held ? ' <span class="plan-qual">may help</span>' : '') + lead + '</span></li>';
  }

  // Issue #1404: a time-of-use bank holding the house right now opens the list ("Now"), in the same words as the
  // Status line, so the list says what the thermostat is told and when the band returns.
  function _planHoldRowHtml() {
    const h = _timeline && _timeline.current_hold;
    if (!h || !h.text) return '';
    return '<li class="plan-row plan-ca plan-now" data-kind="tou_bank" data-actor="ca">' +
      '<span class="plan-time">Now</span>' +
      '<span class="plan-actor" title="Climate Advisor does this">' +
        '<svg width="12" height="12" viewBox="-7 -7 14 14" aria-hidden="true"><circle r="4.5" fill="var(--c-plan)" stroke="var(--c-plan)" stroke-width="1.5"/></svg>CA</span>' +
      '<span class="plan-text">' + escapeHtml(h.text) + '</span></li>';
  }

  // Render the "Next 24 hours" list into `el`. opts.extra() may return extra HTML for the foot (the page's own
  // facts, for example the nat-vent cycling range); it is escaped by the caller.
  function renderPlanList(el, opts) {
    if (!el) return;
    const o = opts || {};
    const now = Date.now();
    const extra = typeof o.extra === 'function' ? (o.extra() || '') : '';
    let body;
    if (_timelineState === 'idle') {
      body = '<li class="plan-empty">Loading the plan...</li>';
    } else if (_timelineState === 'none') {
      body = '<li class="plan-empty">The plan appears after Climate Advisor has classified the day.</li>';
    } else if (_timelineState === 'error') {
      body = '<li class="plan-empty">The plan could not be loaded. It will be retried.</li>';
    } else {
      const steps = _planSteps(PLAN_HORIZON_MS);
      const hold = _planHoldRowHtml();
      body = (steps.length || hold) ? hold + steps.map(s => _planRowHtml(s, now)).join('')
        : '<li class="plan-empty">Nothing is planned for the next 24 hours.</li>';
    }
    el.innerHTML = body;
    const foot = el.parentNode && el.parentNode.querySelector('.plan-foot');
    if (foot) foot.innerHTML = extra;
  }

  function mountPlan(el, opts) {
    if (!el) return;
    _planMounts = _planMounts.filter(m => m.el !== el);
    _planMounts.push({ el: el, opts: opts || {} });
    renderPlanList(el, opts);
  }

  function _renderPlans() { _planMounts.forEach(m => renderPlanList(m.el, m.opts)); }

  async function loadTimeline() {
    try {
      const d = await _host.api('timeline');
      if (d && Array.isArray(d.steps)) { _timeline = d; _timelineState = 'ok'; }
      else { _timeline = null; _timelineState = 'none'; }
    } catch (e) {
      _timeline = null;
      _timelineState = (e && e.status === 503) || /503/.test(String(e && e.message)) ? 'none' : 'error';
    }
    _renderPlans();
    if (_lastChartData) drawChart(_lastChartData);   // the overlay and its key chip read the same plan
    return _timeline;
  }

  function init(host) {
    _host = Object.assign(_host, host || {});
    window.setChartRange = setChartRange;
    window.setChartAhead = setChartAhead;
    window.setChartLead = setChartLead;
    window.resetChartZoom = resetChartZoom;
    window.toggleOverlay = toggleOverlay;
    window.toggleSeries = toggleSeries;
    window.shiftChartWindow = shiftChartWindow;
    window.chartGoNow = chartGoNow;
    window.toggleTableView = toggleTableView;
    window.copyChartTable = copyChartTable;
    window.clearChartPin = clearChartPin;
    _initChartInteraction();
  }

  window.CAChartV2 = {
    init: init,
    load: loadChart,
    setStatus: function (s) { _lastStatus = s; },
    data: function () { return _lastChartData; },
    redraw: function () { if (_lastChartData) drawChart(_lastChartData); },
    loadTimeline: loadTimeline,
    timeline: function () { return _timeline; },
    mountPlan: mountPlan,
    renderPlanList: renderPlanList,
  };
})();
