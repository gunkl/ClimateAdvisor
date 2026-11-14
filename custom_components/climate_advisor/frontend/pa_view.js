/* Climate Advisor - the Prediction accuracy view, shared (Issue #1497).

   One view, two hosts: the stand-alone page (prediction_accuracy.html, with its own header: zone picker and "Back to
   dashboard") and the new dashboard's Analysis tab (new_dashboard.html, mounted straight into the page). Never in a
   frame: the dashboard is already inside Home Assistant's panel frame, and a page framed one level deeper could not
   reach HA's login on a phone (Issue #1491). Like chart_v2.js for the Status chart.

   window.CAPredictionAccuracy.mount(root, { zone, embedded, onZones }) draws the view into root and loads it;
   .setZone(entryId) switches zone (resets sensor, scroll and zoom). One view per document (element ids are fixed).
   Requests: GET zones, then one scoped GET prediction_accuracy?zone=... per control state. Nothing is written. */
(function () {
  'use strict';

  // ---------- view styles (scoped to .pa-view so they never reach the host page) ----------
  var CSS = [
    '.pa-view { display: flex; flex-direction: column; gap: 12px; min-width: 0; }',
    '.pa-view .pa-block { display: flex; flex-direction: column; gap: 10px; min-width: 0; }',
    '.pa-view .pa-block + .pa-block { border-top: 1px solid var(--rule); padding-top: 12px; }',
    '.pa-view h1, .pa-view h2, .pa-view h3 { margin: 0; font-size: 16px; }',
    '.pa-view .sub { font-size: 12px; color: var(--muted); }',
    '.pa-view .row { display: flex; flex-wrap: wrap; gap: 8px 12px; align-items: center; }',
    '.pa-view .grp-label { font-size: 12px; color: var(--muted); }',
    '.pa-view .grp { display: flex; align-items: center; flex-wrap: wrap; border: 1px solid var(--rule); border-radius: 8px; overflow: hidden; }',
    '.pa-view .grp.range { flex-wrap: nowrap; max-width: 100%; overflow-x: auto; }',
    '.pa-view .grp button, .pa-view .reset, .pa-view .retry { min-height: 36px; font: inherit; font-size: 13px; color: var(--ink); }',
    '.pa-view .grp button { padding: 0 11px; border: 0; border-radius: 0; background: transparent; cursor: pointer; }',
    '.pa-view .grp button[aria-pressed="true"] { background: var(--ink); color: var(--panel); }',
    '.pa-view .grp button:disabled { opacity: 0.4; cursor: not-allowed; }',
    /* A sensor with nothing recorded (Issue #1431): aria-disabled, not disabled, so its reason still shows on hover */
    '.pa-view .grp button[aria-disabled="true"] { color: var(--muted); cursor: not-allowed; }',
    '.pa-view .grp button .tag { font-size: 11px; color: var(--muted); }',
    '.pa-view .grp button[aria-pressed="true"] .tag { color: inherit; }',
    '.pa-view .note-v4 { border-inline-start: 3px solid var(--s1); padding: 2px 0; padding-inline-start: 8px; }',
    '.pa-view .grp button.nav { min-width: 36px; font-size: 16px; padding: 0 8px; }',
    '.pa-view .reset, .pa-view .retry { padding: 0 12px; border-radius: 8px; border: 1px solid var(--rule); background: transparent; cursor: pointer; }',
    '.pa-view button:focus-visible, .pa-view canvas:focus-visible { outline: 2px solid var(--s1); outline-offset: 2px; }',
    '.pa-view .grow { flex: 1 1 0; }',
    '.pa-view .small { font-size: 12px; color: var(--ink2); }',
    '.pa-view .legend { display: flex; flex-wrap: wrap; gap: 6px 16px; font-size: 12px; color: var(--ink2); align-items: center; }',
    '.pa-view .legend span.k { display: flex; align-items: center; gap: 6px; }',
    '.pa-view .hover { color: var(--ink); font-variant-numeric: tabular-nums; min-height: 1.2em; }',
    /* Time runs left to right in every language (as most RTL apps draw charts): the plot, its ticks and the range strip keep LTR */
    '.pa-view .chart-wrap, .pa-view .grp.range { direction: ltr; }',
    '.pa-view .chart-wrap { position: relative; height: 320px; touch-action: pan-y; }',
    '.pa-view .chart-wrap canvas { cursor: crosshair; }',
    '.pa-view .overlay { position: absolute; inset: 0; display: flex; align-items: center; justify-content: center; text-align: center; padding: 20px; color: var(--muted); font-size: 14px; pointer-events: none; }',
    '.pa-view .tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 8px; }',
    '.pa-view .tile { border: 1px solid var(--rule); border-radius: 10px; padding: 8px 10px; background: transparent; }',
    '.pa-view .tile .l { font-size: 12px; color: var(--muted); }',
    '.pa-view .tile .v { font-size: 20px; font-weight: 650; font-variant-numeric: tabular-nums; }',
    '.pa-view .tile .n { font-size: 12px; color: var(--ink2); }',
    '.pa-view .state { border: 1px solid var(--rule); border-radius: 10px; padding: 12px 14px; font-size: 14px; }',
    '.pa-view .state.err { border-color: var(--warn); color: var(--ink); }',
    '.pa-view .state.err strong { color: var(--warn); }',
    '.pa-view .state.empty { background: var(--chip); }',
    '.pa-view .scroll-x { overflow-x: auto; }',
    '.pa-view table { border-collapse: collapse; width: 100%; min-width: 760px; font-size: 13px; font-variant-numeric: tabular-nums; }',
    '.pa-view td, .pa-view th { padding: 6px 8px; border-top: 1px solid var(--rule); text-align: start; vertical-align: top; }',
    '.pa-view th { font-weight: 500; color: var(--muted); border-top: 0; }',
    '.pa-view td.yes { font-weight: 600; }',
    '.pa-view td.no { opacity: 0.7; }',
    '.pa-view td.na { color: var(--muted); }',
    '.pa-view .verdict { font-size: 12px; color: var(--warn); }',
    '.pa-view .sr-only { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; }',
    '.pa-view [hidden] { display: none !important; }'
  ].join('\n');
  function injectCss() {
    if (document.getElementById('pa-view-css')) return;
    var st = document.createElement('style');
    st.id = 'pa-view-css'; st.textContent = CSS;
    document.head.appendChild(st);
  }

  // ---------- view markup ----------
  // The page wraps the two parts in cards with h1/h2; embedded (the dashboard's Analysis card, which has its own
  // "Prediction accuracy" heading) they are plain blocks and the title is visually hidden. Ids are the same in both.
  function markup(embedded) {
    var e = function (k, p) { return esc(T(k, p)); };
    var title = embedded
      ? '<h3 id="pa-title" class="sr-only">' + e('title_chart_sr') + '</h3>'
      : '<h1 id="pa-title">' + e('title') + '</h1>';
    var gateTitle = embedded
      ? '<h3 id="gate-title">' + e('gate.title') + '</h3>'
      : '<h2 id="gate-title">' + e('gate.title') + '</h2>';
    var open = function (lbl) {
      return embedded ? '<div class="pa-block" role="group" aria-labelledby="' + lbl + '">' : '<section class="card" aria-labelledby="' + lbl + '">';
    };
    var close = embedded ? '</div>' : '</section>';
    var line = function (extra, dash) {
      return '<svg width="22" height="8" aria-hidden="true"><line x1="0" y1="4" x2="22" y2="4" stroke="currentColor" ' + extra + '></line></svg>';
    };
    return [
      open('pa-title'),
      '    <div class="row" style="justify-content: space-between">',
      '      <div>',
      '        ' + title,
      '        <div class="sub" id="pa-sub">' + e('sub', { lead: leadText(S.lead) }) + '</div>',
      '      </div>',
      '      <div class="row">',
      '        <div class="row" style="gap: 6px"><span class="grp-label" id="lbl-sensor">' + e('sensor_label') + '</span>',
      '          <div class="grp" role="group" aria-labelledby="lbl-sensor" id="grp-sensor"></div></div>',
      '        <div class="row" style="gap: 6px"><span class="grp-label" id="lbl-lead">' + e('predicted') + '</span>',
      '          <div class="grp" role="group" aria-label="' + e('lead_group_aria') + '" id="grp-lead"></div></div>',
      '      </div>',
      '    </div>',
      '',
      '    <div class="row">',
      '      <div class="grp range" role="group" aria-label="' + e('range_aria') + '" id="grp-range"></div>',
      '      <button type="button" class="reset" id="reset-zoom" hidden>' + e('reset_zoom') + '</button>',
      '      <span class="small" id="window-label"></span>',
      '      <span class="grow"></span>',
      '      <span class="grp-label">' + e('drag_hint') + '</span>',
      '    </div>',
      '',
      '    <div id="state-box" aria-live="polite"></div>',
      '',
      '    <div class="legend" id="legend">',
      '      <span class="k">' + line('stroke-width="2"') + e('legend.actual') + '</span>',
      '      <span class="k" style="color: var(--s1)">' + line('stroke-width="2.5"') + '<span id="legend-v4-name">v4</span></span>',
      '      <span class="k" id="legend-v3" style="color: var(--s2)">' + line('stroke-width="2" stroke-dasharray="5 3"') + '<span id="legend-v3-name">v3</span></span>',
      '      <span class="k" id="legend-flat" style="color: var(--muted)">' + line('stroke-width="1.5" stroke-dasharray="1 3" stroke-linecap="round"') + e('legend.flat') + '</span>',
      '      <span class="k" id="legend-swap" style="color: var(--muted)" hidden><svg width="10" height="12" aria-hidden="true"><line x1="5" y1="0" x2="5" y2="12" stroke="currentColor" stroke-width="1.5" stroke-dasharray="2 2"></line></svg>' + e('legend.swap') + '</span>',
      '      <span class="k" style="color: var(--heat)"><svg width="14" height="8" aria-hidden="true"><rect width="14" height="8" rx="2" fill="currentColor" opacity="0.6"></rect></svg>' + e('legend.heat') + '</span>',
      '      <span class="grow"></span>',
      '      <span class="hover" id="hover" aria-live="off"></span>',
      '    </div>',
      '',
      '    <div class="chart-wrap" id="chart-wrap">',
      '      <canvas id="pa-chart" tabindex="0" role="img" aria-label="' + e('chart_aria_default') + '"></canvas>',
      '      <div class="overlay" id="chart-overlay" hidden></div>',
      '    </div>',
      '    <div class="small" id="pa-explain"></div>',
      '    <div class="small note-v4" id="pa-nov4" role="status" hidden></div>',
      '',
      '    <div class="tiles" id="tiles"></div>',
      '    <div class="small" id="tile-note"></div>',
      '  ' + close,
      '',
      '  ' + open('gate-title'),
      '    <div class="row" style="justify-content: space-between">',
      '      ' + gateTitle,
      '      <span class="verdict" id="gate-verdict"></span>',
      '    </div>',
      '    <div class="sub"><strong id="gate-window">' + e('gate.window', { days: 30 }) + '</strong></div>',
      '    <div class="sub" id="gate-sub">' + e('gate.sub', { n: GATE_MIN_N }) + '</div>',
      '    <div class="scroll-x">',
      '      <table id="gate-table">',
      '        <thead><tr><th>' + e('gate.th.zone') + '</th><th>' + e('gate.th.season') + '</th><th>' + e('gate.th.lead') + '</th><th>' + e('gate.th.v4') + '</th><th>' + e('gate.th.v3') + '</th><th>' + e('legend.flat') + '</th><th>' + e('gate.th.bias') + '</th><th>' + e('gate.th.points') + '</th><th>' + e('gate.th.beats_v3') + '</th><th>' + e('gate.th.beats_flat') + '</th><th id="th-bias">' + e('gate.th.within') + '</th></tr></thead>',
      '        <tbody id="gate-body"></tbody>',
      '      </table>',
      '    </div>',
      '    <div class="small" id="seasons-note">' + e('gate.seasons_note') + '</div>',
      '  ' + close,
      '  <div class="sub">' + e('gate.footer') + '</div>'
    ].join('\n');
  }

  var API_PATH = '/api/climate_advisor/prediction_accuracy';
  var ZONE_STORAGE_KEY = 'climate_advisor_selected_zone'; // same key as index.html
  var CA_SENSOR = '_ca';
  var LEADS = ['1h', '4h', '12h', '24h', '48h', '72h', '120h'];  // Issue #1483: and 2, 3 and 5 days (live archive only)
  var RANGES = ['6h', '12h', '24h', '3d', '7d', '30d', '1y'];
  var RANGE_MS = {
    '6h': 6 * 3600e3, '12h': 12 * 3600e3, '24h': 24 * 3600e3, '3d': 72 * 3600e3,
    '7d': 168 * 3600e3, '30d': 720 * 3600e3, '1y': 8760 * 3600e3
  };
  var GATE_MIN_N = 72;  // Issue #1492: points a live scorecard row needs before it is judged (the server's min_n wins)
  var BIAS_LIMIT_F = 0.5;

  var S = {
    zone: null, zones: [], sensor: CA_SENSOR, lead: '4h', range: '3d', before: null,
    sensors: [], data: null, loading: false, error: null, zoom: null, hoverIdx: null
  };
  var chart = null, reqSeq = 0, reqCtl = null;

  function $(id) { return document.getElementById(id); }
  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }
  // Issue #1341: catalog text (dashboard.pa.*). T() renders through CAI18N (params may be typed wire values), the
  // result is escaped by the caller (esc()) or set with textContent. iso() isolates a dynamic Latin name in RTL.
  function T(key, params) { return window.CAI18N ? window.CAI18N.t('dashboard.pa.' + key, params) : key; }
  function iso(x) { return window.CAI18N && window.CAI18N.dir === 'rtl' ? '\u2068' + x + '\u2069' : String(x); }
  function lang() { return (window.CAI18N && window.CAI18N.lang) || 'en'; }
  // Intl formatter for the dashboard language that honours the user's 12/24 h preference (the browser locale is not
  // used: the page language is the dashboard's, as for every other formatter).
  function dtf(opts) {
    var o = Object.assign({}, opts), loc = window.CAI18N && window.CAI18N.locale;
    if (o.hour && loc && (loc.time_format === '12' || loc.time_format === '24')) o.hour12 = loc.time_format === '12';
    try { return new Intl.DateTimeFormat(lang(), o); } catch (e) { return new Intl.DateTimeFormat(undefined, o); }
  }
  function nf(v, d) {
    var o = d == null ? { maximumFractionDigits: 6, useGrouping: false } : { minimumFractionDigits: d, maximumFractionDigits: d, useGrouping: false };
    return window.CAI18N ? window.CAI18N.fmt.number(Number(v), o) : Number(v).toFixed(d == null ? 2 : d);
  }
  function cssVar(n) { return getComputedStyle(document.documentElement).getPropertyValue(n).trim(); }
  // the zoom-drag tint: the page defines --sel; the dashboard has no such token, so fall back to the page's light value
  function selColor() { return cssVar('--sel') || 'rgba(42, 120, 214, 0.12)'; }

  // ---------- auth (same approach as index.html) ----------
  // Issue #1491: HA's <home-assistant> is normally the parent (the panel frame); also look in the top window in case
  // this page is ever one frame deeper. The localStorage 'hassTokens' fallback is never written by the iOS app.
  function hassAuth() {
    var wins = [window.parent, window.top];
    for (var i = 0; i < wins.length; i++) {
      try {
        var ha = wins[i].document.querySelector('home-assistant');
        if (ha && ha.hass && ha.hass.auth) return ha.hass.auth;
      } catch (e) { /* not embedded or cross-origin */ }
    }
    return null;
  }
  async function getAuthToken() {
    var auth = hassAuth();
    if (auth) {
      if (auth.data && auth.data.expires && auth.data.expires - Date.now() < 300000) {
        try { await auth.refreshAccessToken(); } catch (e) { /* refresh failed */ }
      }
      if (auth.data && auth.data.access_token) return auth.data.access_token;
    }
    try {
      var stored = localStorage.getItem('hassTokens');
      if (stored) {
        var t = JSON.parse(stored);
        if (t && t.access_token) return t.access_token;
      }
    } catch (e) { /* unavailable */ }
    return null;
  }
  async function forceTokenRefresh() {
    var auth = hassAuth();
    if (auth) {
      try {
        await auth.refreshAccessToken();
        if (auth.data && auth.data.access_token) return auth.data.access_token;
      } catch (e) { /* refresh failed */ }
    }
    return null;
  }
  async function authedFetch(url, signal) {
    var token = await getAuthToken();
    var headers = {};
    if (token) headers['Authorization'] = 'Bearer ' + token;
    var resp = await fetch(url, { credentials: 'same-origin', headers: headers, signal: signal });
    if (resp.status === 401) {
      var fresh = await forceTokenRefresh();
      if (fresh && fresh !== token) {
        resp = await fetch(url, { credentials: 'same-origin', headers: { 'Authorization': 'Bearer ' + fresh }, signal: signal });
      }
    }
    if (!resp.ok) {
      var msg = 'HTTP ' + resp.status;
      try { var b = await resp.json(); if (b && b.message) msg = b.message; } catch (e) { /* ignore */ }
      var err = new Error(msg); err.status = resp.status;
      throw err;
    }
    return resp.json();
  }

  // ---------- request building ----------
  // One request per control state: zone, sensor, lead, range and (when scrolled back) before.
  function buildQuery(st) {
    var p = new URLSearchParams();
    if (st.zone) p.set('zone', st.zone);
    p.set('sensor', st.sensor);
    p.set('lead', st.lead);
    p.set('range', st.range);
    if (st.before) p.set('before', st.before);
    return p.toString();
  }
  function buildUrl(st) { return API_PATH + '?' + buildQuery(st); }

  // ---------- formatting ----------
  function unit() { return (S.data && S.data.unit) || '°F'; }
  function isC() { return /C/.test(unit()); }
  function fnum(v, d) { return v == null || isNaN(v) ? '—' : nf(v, d == null ? 2 : d); }
  function fmiss(v) { return v == null ? '—' : fnum(v) + ' ' + unit(); }
  function fbias(v) { return v == null ? '—' : (v > 0 ? '+' : '') + fnum(v) + ' ' + unit(); }
  function leadText(l) {
    var h = parseInt(l, 10);
    return h >= 48 && h % 24 === 0 ? T('lead.days', { n: h / 24 }) : T('lead.hours', { n: h });
  }
  function fmtT(ms) {
    return dtf({ weekday: 'short', month: 'numeric', day: 'numeric', hour: 'numeric', minute: '2-digit' }).format(new Date(ms));
  }
  function toMs(t) { var v = Date.parse(t); return isNaN(v) ? null : v; }

  // ---------- window / data helpers ----------
  function pts() {
    if (!S.data || !Array.isArray(S.data.points)) return [];
    var out = [];
    S.data.points.forEach(function (p) {
      var ms = toMs(p.t);
      if (ms != null) out.push({ ms: ms, a: p.actual == null ? null : p.actual, v4: p.v4 == null ? null : p.v4, v3: p.v3 == null ? null : p.v3, f: p.flat == null ? null : p.flat });
    });
    out.sort(function (a, b) { return a.ms - b.ms; });
    return out;
  }
  function windowMs() {
    var d = S.data;
    var s = d && toMs(d.start), e = d && toMs(d.end);
    if (s == null || e == null) {
      e = S.before ? toMs(S.before) : Date.now();
      s = e - RANGE_MS[S.range];
    }
    return [s, e];
  }
  function viewWindow() { return S.zoom ? S.zoom : windowMs(); }
  function hasV3() {
    return S.sensor === CA_SENSOR && pts().some(function (p) { return p.v3 != null; });
  }
  function scoreInView() {
    // Time in view: server stats for the whole window, local scoring once the viewer zooms in.
    var w = viewWindow();
    if (!S.zoom && S.data && S.data.stats) {
      var st = S.data.stats;
      return { n: st.n, n_v3: st.n_v3, n_flat: st.n_flat, v4_mae: st.v4_mae, v3_mae: st.v3_mae, flat_mae: st.flat_mae, v4_bias: st.v4_bias, v3_bias: st.v3_bias, local: false };
    }
    // Each engine is scored on its own points (Issue #1400): v3 and "no change" score even where v4 has none.
    var inView = pts().filter(function (p) { return p.ms >= w[0] && p.ms <= w[1] && p.a != null; });
    function own(k) { return inView.filter(function (p) { return p[k] != null; }); }
    function mae(k) {
      var g = own(k);
      return g.length ? g.reduce(function (s, p) { return s + Math.abs(p[k] - p.a); }, 0) / g.length : null;
    }
    function bias(k) {
      var g = own(k);
      return g.length ? g.reduce(function (s, p) { return s + (p[k] - p.a); }, 0) / g.length : null;
    }
    return { n: own('v4').length, n_v3: own('v3').length, n_flat: own('f').length, v4_mae: mae('v4'), v3_mae: mae('v3'), flat_mae: mae('f'), v4_bias: bias('v4'), v3_bias: bias('v3'), local: true };
  }

  // ---------- controls ----------
  function segButton(label, pressed, onClick, opts) {
    var b = document.createElement('button');
    b.type = 'button';
    b.textContent = label;
    b.setAttribute('aria-pressed', pressed ? 'true' : 'false');
    if (opts && opts.disabled) b.disabled = true;
    if (opts && opts.cls) b.className = opts.cls;
    if (opts && opts.aria) b.setAttribute('aria-label', opts.aria);
    if (opts && opts.value) b.dataset.value = opts.value;
    b.addEventListener('click', onClick);
    return b;
  }
  function sensorList() {
    var ca = (S.sensors || []).filter(function (s) { return s && s.id === CA_SENSOR; })[0];
    // name/role: the backend's `name_msg` / `role_msg` wire siblings (lane L4) when present, else its English field
    function wired(w, plain) { var r = w && window.CAI18N ? window.CAI18N.tw(w) : ''; return r || plain; }
    var list = [{ id: CA_SENSOR, name: wired(ca && ca.name_msg, (ca && ca.name) || T('sensor.default_name')), role: wired(ca && ca.role_msg, ca && ca.role), has_v4: ca && ca.has_v4, has_readings: ca && ca.has_readings }];
    (S.sensors || []).forEach(function (s) {
      if (s && s.id && s.id !== CA_SENSOR) list.push({ id: s.id, name: wired(s.name_msg, s.name || s.id), role: wired(s.role_msg, s.role), has_v4: s.has_v4, has_readings: s.has_readings });
    });
    return list;
  }
  function currentSensor() { return sensorList().filter(function (x) { return x.id === S.sensor; })[0] || null; }
  // Issue #1431: what a sensor button can show. `has_v4` / `has_readings` come from the payload (absent = an older
  // backend: unknown, so the button stays as it was). "_ca" is always selectable: its own states explain an empty model.
  function sensorAvail(s) {
    if (s.id !== CA_SENSOR && s.has_v4 === false && s.has_readings === false) {
      return { off: true, tag: T('sensor.tag_no_data'), why: T('sensor.why_no_data') };
    }
    if (s.has_v4 === false) {
      return { off: false, tag: T('sensor.tag_no_v4'), why: T('sensor.why_no_v4') };
    }
    return { off: false, tag: '', why: '' };
  }
  // The engines' roles (Settings, Prediction engine) from the payload: primary = "main engine", shadow = "comparison".
  // The role as a CODE ('main' | 'comparison' | 'off' | ''), never English prose: the text is role.<code>.
  function engineRole(id) {
    var d = S.data;
    if (!d) return '';
    var primary = d.primary_engine || 'v4', shadow = d.shadow_engine || 'none';
    if (primary === id) return 'main';
    if (shadow === id) return 'comparison';
    return 'off';
  }
  function engineName(id) { var r = engineRole(id); return r ? T('engine.named', { id: id, role: T('role.' + r) }) : id; }
  function renderControls() {
    var gs = $('grp-sensor'); gs.textContent = '';
    sensorList().forEach(function (s) {
      var av = sensorAvail(s);
      var b = segButton(s.name, S.sensor === s.id, function () { if (!av.off) pickSensor(s.id); }, { value: s.id });
      if (av.tag) {
        var tag = document.createElement('span');
        tag.className = 'tag'; tag.textContent = ' · ' + av.tag;
        b.appendChild(tag);
      }
      if (av.off) b.setAttribute('aria-disabled', 'true');
      b.title = [s.role, av.why].filter(Boolean).join(' — ');
      gs.appendChild(b);
    });
    $('legend-v4-name').textContent = engineName('v4');
    $('legend-v3-name').textContent = engineName('v3');
    var gl = $('grp-lead'); gl.textContent = '';
    LEADS.forEach(function (l) {
      gl.appendChild(segButton(T('lead.ahead', { lead: leadText(l) }), S.lead === l, function () { pickLead(l); }, { value: l }));
    });
    var gr = $('grp-range'); gr.textContent = '';
    var back = segButton('‹', false, function () { scroll(-1); }, { cls: 'nav', aria: T('nav.back'), disabled: !S.data });
    back.id = 'nav-back';
    gr.appendChild(back);
    RANGES.forEach(function (r) {
      gr.appendChild(segButton(r, S.range === r, function () { pickRange(r); }, { value: r }));
    });
    var fwd = segButton('›', false, function () { scroll(1); }, { cls: 'nav', aria: T('nav.forward'), disabled: !S.before });
    fwd.id = 'nav-fwd';
    gr.appendChild(fwd);
    // Nav arrows are plain buttons, not toggles.
    back.removeAttribute('aria-pressed'); fwd.removeAttribute('aria-pressed');
    $('reset-zoom').hidden = !S.zoom;
    $('legend-v3').hidden = S.sensor !== CA_SENSOR;
    $('legend-flat').hidden = !(S.data && hasFlat());
    $('legend-swap').hidden = !swapsInView().length;
    $('lbl-lead').textContent = T('predicted');
    $('pa-sub').textContent = T('sub', { lead: leadText(S.lead) });
  }

  function pickSensor(id) { S.sensor = id; S.zoom = null; load(); }
  function pickLead(l) { S.lead = l; S.zoom = null; load(); }
  function pickRange(r) { S.range = r; S.before = null; S.zoom = null; load(); }
  function scroll(dir) {
    var span = RANGE_MS[S.range];
    var end = S.before ? toMs(S.before) : Date.now();
    var next = end + dir * span;
    S.zoom = null;
    if (dir > 0 && next >= Date.now() - span / 20) S.before = null; // snap back to live
    else S.before = new Date(next).toISOString();
    load();
  }
  function resetZoom() {
    S.zoom = null;
    if (chart && chart.resetZoom) chart.resetZoom();
    renderAll();
  }
  // Read-only view of what the chart draws, for the UI tests (Issue #1431): each line's points (null = a break).
  window.__paLines = function () {
    return chart ? chart.data.datasets.map(function (d) { return { label: d.label, data: d.data.map(function (q) { return q.y == null ? null : q.x; }), gapLimitMs: d.gapLimitMs }; }) : [];
  };

  // ---------- loading ----------
  // Issue #1454: every request names a zone. The zone list comes from GET zones (names no zone by design, raises
  // nothing); with no zone loaded yet (Climate Advisor still starting) nothing is sent. A failed request (a startup 503,
  // a 404 before the views are registered) re-reads the zone list and retries the same zone after 2 s, 5 s and 10 s,
  // then says Climate Advisor is starting. An unscoped request raised the "Ambiguous zone selection" Repairs card.
  var BACKOFF_MS = [2000, 5000, 10000], retryTimer = null;
  function starting() { return T('state.starting'); }
  async function load(attempt) {
    attempt = typeof attempt === 'number' ? attempt : 0;
    if (retryTimer) { clearTimeout(retryTimer); retryTimer = null; }
    var seq = ++reqSeq;
    if (reqCtl) { try { reqCtl.abort(); } catch (e) { /* ignore */ } }
    reqCtl = (typeof AbortController !== 'undefined') ? new AbortController() : null;
    S.loading = true; S.error = null;
    renderAll();
    try {
      if (!S.zone) await loadZones();
      if (seq !== reqSeq) return;
      if (!S.zone) { var se = new Error(starting()); se.starting = true; throw se; }
      var d = await authedFetch(buildUrl(S), reqCtl ? reqCtl.signal : undefined);
      if (seq !== reqSeq) return;
      S.data = d;
      if (Array.isArray(d.sensors)) S.sensors = d.sensors;
      S.loading = false;
    } catch (e) {
      if (seq !== reqSeq || (e && e.name === 'AbortError')) return;
      var retryable = e.starting || !e.status || e.status === 404 || e.status >= 500;
      if (retryable && attempt < BACKOFF_MS.length) {
        var before = S.zone;
        await loadZones();
        if (seq !== reqSeq) return;
        retryTimer = setTimeout(function () { load(attempt + 1); }, S.zone && S.zone !== before ? 0 : BACKOFF_MS[attempt]);
        return; // still "Loading…"
      }
      S.loading = false;
      S.error = retryable ? starting() : ((e && e.message) ? e.message : T('state.request_failed'));
    }
    renderAll();
  }

  async function loadZones() {
    try {
      var st = await authedFetch('/api/climate_advisor/zones');
      var zones = st && Array.isArray(st.zones) ? st.zones : [];
      if (!zones.length) return; // none loaded yet (starting): keep the zone, it is listed again once it is up
      S.zones = zones;
      if (!S.zone || !zones.some(function (z) { return z.entry_id === S.zone; })) {
        S.zone = zones[0].entry_id;
        try { localStorage.setItem(ZONE_STORAGE_KEY, S.zone); } catch (e) { /* ignore */ }
      }
      if (hooks.onZones) hooks.onZones(zones, S.zone); // the page's own zone picker (the dashboard has its own)
    } catch (e) { /* zones not served yet (starting): load() waits and retries */ }
  }

  // ---------- rendering ----------
  function renderAll() {
    renderControls();
    renderState();
    renderChart();
    renderExplain();
    renderTiles();
    renderGate();
  }

  function isEmptyModel() {
    var d = S.data;
    if (!d) return false;
    var noPts = !Array.isArray(d.points) || d.points.length === 0;
    var conf = d.model && d.model.confidence;
    return noPts && (!d.model || conf === 'none' || conf == null);
  }

  function renderState() {
    var box = $('state-box'); box.textContent = '';
    var overlay = $('chart-overlay');
    overlay.hidden = true;
    if (S.error) {
      var e = document.createElement('div');
      e.className = 'state err'; e.setAttribute('role', 'alert');
      e.innerHTML = '<strong>' + esc(T('state.error_title')) + '</strong> ' + esc(S.error) + ' ';
      var r = document.createElement('button');
      r.type = 'button'; r.className = 'retry'; r.id = 'retry'; r.textContent = T('state.retry');
      r.addEventListener('click', function () { load(); });
      e.appendChild(r);
      box.appendChild(e);
      return;
    }
    if (S.loading && !S.data) { overlay.hidden = false; overlay.textContent = T('state.loading'); return; }
    if (isEmptyModel()) {
      var m = document.createElement('div');
      m.className = 'state empty'; m.id = 'empty-state';
      m.textContent = T('state.empty_model');
      box.appendChild(m);
      return;
    }
    if (S.data && pts().length === 0) {
      overlay.hidden = false; overlay.textContent = T('state.nothing_in_window');
    }
  }

  // Issue #1431: one plain line under the chart saying what the points are, and why v4 has no line when it has none.
  function renderExplain() {
    var ex = $('pa-explain'), nv = $('pa-nov4');
    if (!S.data || S.error) { ex.textContent = ''; nv.hidden = true; return; }
    ex.textContent = T('explain', { lead: leadText(S.lead), swaps: swapsInView().length ? 'yes' : 'no' });
    var msg = '';
    if (!isEmptyModel() && !pts().some(function (p) { return p.v4 != null; })) {
      var cur = currentSensor();
      if (cur && cur.has_v4 === false) msg = T('nov4.not_learned');
      else if (engineRole('v4') === 'off') msg = T('nov4.not_running');
      else msg = T(cur && cur.has_v4 ? 'nov4.none_elsewhere' : 'nov4.none');
    }
    nv.textContent = msg; nv.hidden = !msg;
  }

  function readColors() {
    return {
      ink: cssVar('--ink'), ink2: cssVar('--ink2'), muted: cssVar('--muted'), rule: cssVar('--rule'),
      s1: cssVar('--s1'), s2: cssVar('--s2'), heat: cssVar('--heat'), chip: cssVar('--chip')
    };
  }

  function tickLabel(ms) {
    var w = windowMs(), span = w[1] - w[0], d = new Date(ms);
    if (span <= 36 * 3600e3) return dtf({ hour: 'numeric' }).format(d);
    if (span <= 8 * 24 * 3600e3) return dtf({ weekday: 'short', month: 'numeric', day: 'numeric', hour: 'numeric' }).format(d);
    return dtf({ month: 'numeric', day: 'numeric' }).format(d);
  }

  // Draws the heat-run strip along the bottom of the plot, a "no data" shade, and the hover cursor.
  var overlayPlugin = {
    id: 'paOverlay',
    beforeDatasetsDraw: function (c) {
      var xs = c.scales.x, ca = c.chartArea, ctx = c.ctx, col = readColors();
      if (!xs || !ca) return;
      ctx.save();
      ctx.beginPath(); ctx.rect(ca.left, ca.top, ca.right - ca.left, ca.bottom - ca.top); ctx.clip();
      var p = pts();
      if (S.data && p.length) {
        var first = p[0].ms, last = p[p.length - 1].ms;
        ctx.fillStyle = col.chip;
        if (first > xs.min) { var x1 = Math.min(xs.getPixelForValue(first), ca.right); ctx.fillRect(ca.left, ca.top, x1 - ca.left, ca.bottom - ca.top); }
        if (last < xs.max) { var x0 = Math.max(xs.getPixelForValue(last), ca.left); ctx.fillRect(x0, ca.top, ca.right - x0, ca.bottom - ca.top); }
      }
      var runs = (S.data && S.data.heat_runs) || [];
      ctx.fillStyle = col.heat; ctx.globalAlpha = 0.6;
      runs.forEach(function (r) {
        var a = toMs(r[0]), b = toMs(r[1]);
        if (a == null || b == null || b < xs.min || a > xs.max) return;
        var xa = xs.getPixelForValue(Math.max(a, xs.min)), xb = xs.getPixelForValue(Math.min(b, xs.max));
        ctx.fillRect(xa, ca.bottom - 8, Math.max(2, xb - xa), 8);
      });
      ctx.globalAlpha = 1;
      // Sensor swaps (Issue #1431): a dashed mark where the "_ca" lines break.
      var sw = swapMs();
      if (sw.length) {
        ctx.strokeStyle = col.muted; ctx.lineWidth = 1; ctx.setLineDash([3, 3]);
        sw.forEach(function (m) {
          if (m < xs.min || m > xs.max) return;
          var sx = xs.getPixelForValue(m);
          ctx.beginPath(); ctx.moveTo(sx, ca.top); ctx.lineTo(sx, ca.bottom); ctx.stroke();
        });
        ctx.setLineDash([]);
      }
      if (S.hoverIdx != null) {
        var hp = pts()[S.hoverIdx];
        if (hp) {
          var hx = xs.getPixelForValue(hp.ms);
          ctx.strokeStyle = col.ink2; ctx.lineWidth = 1;
          ctx.beginPath(); ctx.moveTo(hx, ca.top); ctx.lineTo(hx, ca.bottom); ctx.stroke();
        }
      }
      ctx.restore();
    }
  };

  // ---------- lines (Issue #1431) ----------
  // The sensor swaps (bedtime / wake-up) in this window: only the "_ca" lines follow the sensor CA reads, so only they
  // break there (the Status chart's rule: payload `ca_swaps`, none at daily resolution).
  function swapMs() {
    if (S.sensor !== CA_SENSOR || !S.data || !Array.isArray(S.data.ca_swaps)) return [];
    return S.data.ca_swaps.map(toMs).filter(function (v) { return v != null; }).sort(function (a, b) { return a - b; });
  }
  function swapsInView() { var w = viewWindow(); return swapMs().filter(function (m) { return m >= w[0] && m <= w[1]; }); }
  // The spacing the points are meant to have: one per 30-minute slot (raw), per hour (7 d / 30 d) or per day (1 y).
  function baseStepMs() {
    var ds = S.data && S.data.downsample;
    return ds === 'daily' ? 864e5 : (ds === 'hourly' ? 36e5 : 18e5);
  }
  function medianStepMs(own) {
    var g = [];
    for (var i = 1; i < own.length; i++) { var d = own[i].x - own[i - 1].x; if (d > 0) g.push(d); }
    if (!g.length) return null;
    g.sort(function (a, b) { return a - b; });
    return g[g.length >> 1];
  }
  // One series' own points (no other series' slots in between), joined into a line. A null ends the line at a real
  // gap (more than 1.5x the expected spacing: the slot length, or the series' own median spacing up to an hour, so
  // the backtest's hourly points still join) and just before every swap. A point with no neighbour is a dot.
  function lineOf(k, breaks) {
    var own = [];
    pts().forEach(function (q) { if (q[k] != null) own.push({ x: q.ms, y: q[k] }); });
    var base = baseStepMs(), med = medianStepMs(own) || base;
    var expected = Math.min(Math.max(med, base), Math.max(base, 36e5)), limit = 1.5 * expected;
    var data = [], m = 0;
    own.forEach(function (q, i) {
      var cut = false;
      if (i > 0 && q.x - own[i - 1].x > limit) cut = true;
      while (m < breaks.length && breaks[m] <= q.x) { if (i > 0 && breaks[m] > own[i - 1].x) cut = true; m++; }
      if (cut) data.push({ x: q.x - 1, y: null });
      data.push(q);
    });
    var dots = data.map(function (q, i) {
      if (q.y == null) return false;
      var prev = i > 0 && data[i - 1].y != null, next = i < data.length - 1 && data[i + 1].y != null;
      return !prev && !next;
    });
    return { data: data, dots: dots, limit: limit };
  }
  function lineSet(label, k, breaks, style) {
    var l = lineOf(k, breaks);
    return Object.assign({
      label: label, data: l.data, spanGaps: false, borderCapStyle: 'round',
      pointRadius: function (c) { return l.dots[c.dataIndex] ? 3 : 0; },
      pointHoverRadius: function (c) { return l.dots[c.dataIndex] ? 4 : 0; },
      pointBorderWidth: 0, gapLimitMs: l.limit
    }, style);
  }
  function datasets() {
    var col = readColors(), br = swapMs();
    var ds = [
      lineSet(T('legend.actual'), 'a', br, { borderColor: col.ink, borderWidth: 1.6, pointBackgroundColor: col.ink, order: 3 }),
      lineSet('v4', 'v4', br, { borderColor: col.s1, borderWidth: 2.4, pointBackgroundColor: col.s1, order: 1 })
    ];
    if (hasV3()) {
      ds.push(lineSet('v3', 'v3', br, { borderColor: col.s2, borderWidth: 2, borderDash: [6, 4], pointBackgroundColor: col.s2, order: 2 }));
    }
    if (hasFlat()) {
      ds.push(lineSet(T('legend.flat_line'), 'f', br, { borderColor: col.muted, borderWidth: 1.5, borderDash: [1, 4], pointBackgroundColor: col.muted, order: 4 }));
    }
    return ds;
  }
  function hasFlat() { return pts().some(function (p) { return p.f != null; }); }

  function renderChart() {
    var canvas = $('pa-chart');
    var wl = $('window-label');
    if (typeof Chart === 'undefined') { wl.textContent = T('state.chart_failed'); return; }
    var w = viewWindow(), col = readColors();
    var ds = S.data ? datasets() : [];
    var d = S.data;
    wl.textContent = S.data ? windowText(w[0], w[1]) : '';
    canvas.setAttribute('aria-label', T('chart_aria', { unit: unit(), lead: leadText(S.lead) }));
    if (!chart) {
      chart = new Chart(canvas.getContext('2d'), {
        type: 'line',
        data: { datasets: ds },
        plugins: [overlayPlugin],
        options: {
          responsive: true, maintainAspectRatio: false, animation: false, parsing: false, normalized: true,
          layout: { padding: { bottom: 2 } },
          interaction: { mode: 'nearest', axis: 'x', intersect: false },
          plugins: {
            legend: { display: false },
            tooltip: { enabled: false },
            zoom: {
              zoom: {
                drag: { enabled: true, backgroundColor: selColor() },
                pinch: { enabled: true },
                wheel: { enabled: false },
                mode: 'x',
                onZoomComplete: function (ctx) {
                  var xs = ctx.chart.scales.x;
                  S.zoom = [xs.min, xs.max];
                  renderControls(); renderExplain(); renderTiles();
                  $('window-label').textContent = windowText(xs.min, xs.max);
                }
              },
              pan: { enabled: false }
            }
          },
          scales: {
            x: { type: 'linear', ticks: { maxTicksLimit: 8, autoSkip: true, callback: function (v) { return tickLabel(v); } }, grid: {} },
            y: { grace: '5%', ticks: { callback: function (v) { return nf(v) + '°'; } }, grid: {} }
          },
          onHover: function (evt, els, c) { hoverFromEvent(evt, c); }
        }
      });
      canvas.addEventListener('pointerleave', function () { S.hoverIdx = null; $('hover').textContent = ''; if (chart) chart.draw(); });
      canvas.addEventListener('keydown', onKey);
    }
    chart.data.datasets = ds;
    var sx = chart.options.scales.x, sy = chart.options.scales.y;
    sx.min = w[0]; sx.max = w[1];
    sx.ticks.color = col.muted; sy.ticks.color = col.muted;
    sx.grid.color = col.rule; sy.grid.color = col.rule;
    chart.options.plugins.zoom.zoom.drag.backgroundColor = selColor();
    if (!S.zoom && chart.isZoomedOrPanned && chart.isZoomedOrPanned()) chart.resetZoom('none');
    chart.update('none');
    S.hoverIdx = null; $('hover').textContent = '';
  }
  // "<start> to <end>" plus the downsampling note (one message: the order of the parts is the language's own)
  function windowText(a, b) {
    var d = S.data, avg = d && d.downsample === 'hourly' ? 'hourly' : (d && d.downsample === 'daily' ? 'daily' : 'none');
    return T('window.range', { start: iso(fmtT(a)), end: iso(fmtT(b)), avg: avg });
  }

  function showHover(idx) {
    var p = pts()[idx];
    if (!p) return;
    S.hoverIdx = idx;
    var u = unit(), t = [fmtT(p.ms)];
    if (p.a != null) t.push(T('hover.actual', { v: fnum(p.a, 1) + u }));
    if (p.v4 != null) t.push(T('hover.v4', { v: fnum(p.v4, 1) + u }));
    if (p.v3 != null && S.sensor === CA_SENSOR) t.push(T('hover.v3', { v: fnum(p.v3, 1) + u }));
    if (p.f != null) t.push(T('hover.flat', { v: fnum(p.f, 1) + u }));
    $('hover').textContent = t.join(' · ');  // a readout of separate facts, not a sentence
    if (chart) chart.draw();
  }
  function nearestIdx(ms) {
    var p = pts(), best = null, bd = Infinity, w = viewWindow();
    p.forEach(function (q, i) {
      if (q.ms < w[0] || q.ms > w[1]) return;
      var dd = Math.abs(q.ms - ms);
      if (dd < bd) { bd = dd; best = i; }
    });
    return best;
  }
  function hoverFromEvent(evt, c) {
    if (!evt || evt.type === 'mouseout' || evt.x == null) return;
    var xs = c.scales.x;
    var i = nearestIdx(xs.getValueForPixel(evt.x));
    if (i != null) showHover(i);
  }
  function onKey(e) {
    if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') return;
    var p = pts(), w = viewWindow();
    var vis = [];
    p.forEach(function (q, i) { if (q.ms >= w[0] && q.ms <= w[1]) vis.push(i); });
    if (!vis.length) return;
    var pos = S.hoverIdx == null ? -1 : vis.indexOf(S.hoverIdx);
    if (pos < 0) pos = e.key === 'ArrowRight' ? -1 : vis.length;
    pos = Math.max(0, Math.min(vis.length - 1, pos + (e.key === 'ArrowRight' ? 1 : -1)));
    showHover(vis[pos]);
    e.preventDefault();
  }

  function renderTiles() {
    var sc = scoreInView(), v3 = hasV3(), box = $('tiles');
    var selected = (S.data && S.data.primary_engine) === 'v3' ? 'v3' : 'v4'; // the primary engine chosen in Settings leads; the comparison engine follows
    var inView = function (n) { return T('tile.n_predictions', { n: n }); };
    var v4Miss = { l: T('tile.v4_miss'), v: fmiss(sc.v4_mae), n: sc.n ? inView(sc.n) : T('tile.no_v4_scored') };
    var v3Running = !S.data || S.data.primary_engine === 'v3' || S.data.shadow_engine === 'v3';
    var v3Why = S.sensor !== CA_SENSOR ? T('tile.v3_why_sensor')
      : S.lead !== '4h' ? T('tile.v3_why_lead')
      : v3Running ? T('tile.v3_why_none')
      : T('tile.v3_why_off');
    var v3Miss = { l: T('tile.v3_miss'), v: v3 ? fmiss(sc.v3_mae) : '—', n: v3 ? (sc.n_v3 ? T('tile.v3_n', { engine: engineName('v3'), n: sc.n_v3 }) : engineName('v3')) : v3Why };
    var v4Bias = { l: T('tile.v4_bias'), v: fbias(sc.v4_bias), n: v3 ? T('tile.v3_bias_note', { v: fbias(sc.v3_bias) }) : T('tile.in_view') };
    var v3Bias = { l: T('tile.v3_bias'), v: v3 ? fbias(sc.v3_bias) : '—', n: T('tile.v4_bias_note', { v: fbias(sc.v4_bias) }) };
    var tiles = selected === 'v3' ? [v3Miss, v4Miss, v3Bias] : [v4Miss, v3Miss, v4Bias];
    tiles[0].n += ' · ' + T('tile.main_note', { shadow: S.data && S.data.shadow_engine ? S.data.shadow_engine : 'none' });  // separate facts joined by a dot
    if (sc.flat_mae != null) tiles.push({ l: T('legend.flat'), v: fmiss(sc.flat_mae), n: sc.n_flat ? T('tile.flat_n', { n: sc.n_flat }) : T('tile.flat') });
    box.innerHTML = tiles.map(function (t) {
      return '<div class="tile"><div class="l">' + esc(t.l) + '</div><div class="v">' + esc(t.v) + '</div><div class="n">' + esc(t.n) + '</div></div>';
    }).join('');
    var zoomed = sc.local ? 'yes' : 'no', note;
    if (S.sensor !== CA_SENSOR) {
      var sn = sensorList().filter(function (x) { return x.id === S.sensor; })[0];
      note = T('tile.note_sensor', { has_role: sn && sn.role ? 'yes' : 'no', name: sn ? iso(sn.name) : '', role: sn && sn.role ? sn.role : '', zoomed: zoomed });
    } else {
      note = T('tile.note_window', { zoomed: zoomed });
    }
    $('tile-note').textContent = note;
  }

  function zoneName(z) {
    var m = (S.zones || []).filter(function (x) { return x.entry_id === z; })[0];
    return m ? (m.title || z) : z;
  }
  function renderGate() {
    // Issue #1492: the rows are the LIVE scorecard (last 30 days ending now), not the deploy-time backtest.
    var rows = (S.data && Array.isArray(S.data.gate)) ? S.data.gate : [];
    var gateSummary = (S.data && S.data.gate_summary) || null;
    var lim = isC() ? +(BIAS_LIMIT_F * 5 / 9).toFixed(2) : BIAS_LIMIT_F;
    var minN = (gateSummary && gateSummary.min_n) || GATE_MIN_N;
    $('th-bias').textContent = T('gate.th.within_lim', { limit: lim, unit: unit() });
    var days = (gateSummary && gateSummary.window_days) || 30, win;
    if (gateSummary && gateSummary.start && gateSummary.end) {
      var day = function (v) { return dtf({ month: 'short', day: 'numeric' }).format(new Date(v)); };
      win = T('gate.window_range', { days: days, start: iso(day(gateSummary.start)), end: iso(day(gateSummary.end)) });
    } else {
      win = T('gate.window', { days: days });
    }
    $('gate-window').textContent = win;
    var body = $('gate-body');
    if (!rows.length) {
      body.innerHTML = '<tr><td colspan="11" class="na">' + esc(T('gate.none')) + '</td></tr>';
      $('gate-verdict').textContent = T('gate.verdict_none');
      $('gate-verdict').className = 'verdict';
      return;
    }
    var unscored = 0;
    function cell(ok) {
      if (ok == null) return '<td class="na">—</td>';
      return '<td class="' + (ok ? 'yes' : 'no') + '">' + esc(T(ok ? 'gate.yes' : 'gate.no')) + '</td>';
    }
    body.innerHTML = rows.map(function (r) {
      var season = r.season === 'summer' || r.season === 'heating' ? esc(T('gate.season.' + r.season)) : esc(r.season || '');
      var head = '<td>' + esc(zoneName(r.zone)) + '</td><td>' + season + '</td><td>' + esc(leadText(r.lead)) + '</td>';
      // `reasons_msg` (wire list, when the backend sends it) wins over the English `reasons`
      var reasons = Array.isArray(r.reasons_msg) && r.reasons_msg.length && window.CAI18N ? r.reasons_msg.map(function (w) { return window.CAI18N.tw(w); }) : r.reasons;
      var why = (reasons && reasons.length) ? ' title="' + esc(reasons.join('; ')) + '"' : '';
      var n = r.n == null ? 0 : r.n;
      if (r.scored === false) unscored++;
      if (r.scored === false && n < minN) {
        return '<tr' + why + '>' + head + '<td class="na" colspan="8">' + esc(T('gate.not_enough', { n: n, min: minN })) + '</td></tr>';
      }
      var v3 = r.v3_state === 'collecting' ? '<td class="na">' + esc(T('gate.collecting')) + '</td>'
        : r.v3_state === 'off' ? '<td class="na">' + esc(T('gate.v3_off')) + '</td>' : '<td>' + fnum(r.v3) + '</td>';
      if (r.scored === false && r.v3_state !== 'collecting' && r.v3_state !== 'off') {
        return '<tr' + why + '>' + head + '<td>—</td><td>—</td><td>—</td><td>—</td><td>' + esc(nf(n, 0)) + '</td><td class="na" colspan="3">' + esc(T('gate.not_scored')) + '</td></tr>';
      }
      // The server's judgement (beats_v3 / beats_flat / bias_ok) is authoritative; an older payload falls back here.
      var c1 = r.beats_v3 !== undefined ? r.beats_v3 : (r.v3 == null ? null : r.v4 < r.v3);
      var c2 = r.beats_flat !== undefined ? r.beats_flat : r.v4 < r.flat;
      var c3 = r.bias_ok !== undefined ? r.bias_ok : Math.abs(r.bias) <= lim;
      return '<tr' + why + '>' + head + '<td style="font-weight:600">' + fnum(r.v4) + '</td>' + v3 + '<td>' + fnum(r.flat) + '</td><td>' + fbias(r.bias).replace(' ' + unit(), '') + '</td><td>' + esc(nf(n, 0)) + '</td>' + cell(c1) + cell(c2) + cell(c3) + '</tr>';
    }).join('');
    // Descriptive only: how many scored rows v4 beats both v3 and "no change" on. No ready / not-ready language.
    var v = $('gate-verdict');
    v.className = 'verdict';
    if (gateSummary && gateSummary.scored != null) {
      v.textContent = T('gate.verdict', { passed: gateSummary.passed, scored: gateSummary.scored, unscored: unscored });
    } else {
      v.textContent = unscored ? T('gate.verdict_unscored', { n: unscored }) : '';
    }
  }


  // ---------- mount (page and dashboard) ----------
  var hooks = {}, wired = false, catalogsReady = false;
  function mount(root, opts) {
    opts = opts || {};
    hooks = { onZones: opts.onZones || null };
    injectCss();
    root.classList.add('pa-view');
    var ready = window.CAI18N && window.CAI18N.ready;
    if (ready && ready.then && !catalogsReady) {
      // draw once the catalogs are in (CAI18N.ready always resolves); until then the root stays empty
      ready.then(function () { catalogsReady = true; mountNow(root, opts); });
      return;
    }
    mountNow(root, opts);
  }
  function mountNow(root, opts) {
    root.innerHTML = markup(!!opts.embedded);
    if (chart) { try { chart.destroy(); } catch (e) { /* ignore */ } chart = null; }
    if (opts.zone) S.zone = opts.zone;
    $('reset-zoom').addEventListener('click', resetZoom);
    if (!wired) {
      wired = true;
      if (window.matchMedia) {
        var mq = window.matchMedia('(prefers-color-scheme: dark)');
        var onTheme = function () { if (chart) renderChart(); };
        if (mq.addEventListener) mq.addEventListener('change', onTheme); else if (mq.addListener) mq.addListener(onTheme);
      }
      window.addEventListener('resize', function () { if (chart) chart.resize(); });
    }
    renderControls();
    loadZones().then(load);
  }
  function setZone(z) {
    if (!z || z === S.zone) return;
    S.zone = z; S.before = null; S.zoom = null; S.sensors = []; S.sensor = CA_SENSOR; S.data = null;
    load();
  }
  window.CAPredictionAccuracy = { mount: mount, setZone: setZone, zone: function () { return S.zone; } };
})();
