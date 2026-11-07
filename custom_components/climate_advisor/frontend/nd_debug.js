/* Issue #1230 R5: Debug tab - every internal field. Reads automation_state, engines, event_log (diagnostics) and
   prediction_accuracy. Every automation_state key is shown exactly once in a named group, in words (never raw data),
   except promise_summary (shown in Analysis) and any key listed in DOWNLOAD_ONLY. There is no catch-all group:
   tests/test_new_dashboard.py fails when get_debug_state() gains a key that is in none of these lists (Issue #1399). */
(function () {
  'use strict';
  var $ = ND.$, esc = ND.esc;
  var A = null, E = null, EV = null, evOff = {}, loaded = false, timer = null;

  // Group -> automation_state keys. 'Today's classification', 'Learning pipeline', 'Prediction engines' and 'Last thermostat
  // command' are built from nested/other payloads below. Keep in sync with tests/test_new_dashboard.py.
  var GROUPS = [
    ['Control', ['automation_enabled', 'last_action_time', 'last_action_reason', 'next_automation_action', 'next_automation_time', 'decision_lock_holder', 'decision_lock_held_seconds', 'startup_coalesce_active', 'startup_coalesce_seconds_remaining']],
    ['Manual override & grace', ['manual_override_active', 'manual_override_mode', 'manual_override_time', 'manual_grace_duration', 'grace_active', 'grace_end_time', 'grace_stuck', 'last_resume_source', 'resumed_from_pause']],
    ['Doors, windows & pause', ['paused_by_door', 'pre_pause_mode', 'pause_suppressed_classification', 'door_window_sensors', 'pending_debounce_timers']],
    ['Fan', ['fan_mode_config', 'fan_active', 'fan_on_since', 'fan_runtime_minutes', 'thermostat_fan_only_runtime_minutes', 'fan_override_active', 'fan_override_time', 'fan_remote_speed', 'fan_remote_timer_hours', 'fan_remote_timer_ends']],
    ['Natural ventilation & economizer', ['nat_vent_active', 'nat_vent_soft_start', 'nat_vent_ac_assist', 'nat_vent_target', 'nat_vent_on_threshold', 'nat_vent_off_threshold', 'nat_vent_cycling_paused', 'economizer_active', 'economizer_phase']],
    ['Comfort edge', ['comfort_family_armed', 'comfort_family_hold', 'comfort_family_last_switch']],
    ['Occupancy', ['occupancy_mode', 'occupancy_away_timer_pending']],
    ['Units', ['unit', 'configured_unit', 'provider_unit', 'ha_system_unit', 'unit_mismatch', 'ha_unit_mismatch', 'sensor_unit_mismatches']],
    ['Prediction engines (v4 shadow)', ['v4_model', 'control_feed']],
    ['Plan', ['operation_timeline']]
  ];
  var NESTED = { classification: 1, thermal_pipeline: 1 };      // expanded into their own groups
  var MOVED = { promise_summary: 'Analysis' };                    // shown in Analysis, not repeated here
  // Issue #1399: keys deliberately not shown on this tab; their full data is in Download diagnostics (this tab's button
  // saves the whole automation_state; HA's native diagnostics file carries it as debug_state). Every key of
  // get_debug_state() must be in a GROUPS list, NESTED, MOVED or here: tests/test_new_dashboard.py enforces it.
  var DOWNLOAD_ONLY = [];
  // Object-valued keys rendered as plain-language rows by their own builders below (engineRows, planRows).
  var CUSTOM = { v4_model: 1, control_feed: 1, operation_timeline: 1 };
  var DASH = '—';
  var HINTS = {
    occupancy_mode: 'Guest > Vacation > Home/Away', pre_pause_mode: 'HVAC mode before a door paused automation',
    comfort_family_armed: 'Which comfort edge the thermostat is guarding, and the last heat/cool switch',
    comfort_family_hold: 'The thermostat is deliberately not flipping heat/cool yet: the house is only slightly past an edge shortly after the other side ran',
    'classification.day_type': 'Drives HVAC strategy', 'classification.windows_recommended': 'Open windows instead of HVAC',
    'classification.pre_condition': 'Pre-heat/cool before peak', 'classification.setback_modifier': 'Trend-based setpoint adjust',
    'classification.window_open_time': 'Recommended open time', 'classification.window_close_time': 'Recommended close time',
    'classification.today_low': 'Window opportunity needs today low at or below the threshold', 'classification.tomorrow_low': 'Evening opportunity needs tomorrow low at or below the threshold'
  };

  // Issue #1399: no raw JSON on this tab. An object reads "key value · key value", a list of objects "...; ...".
  function fmt(v) {
    if (v == null) return DASH;
    if (typeof v === 'boolean') return v ? 'yes' : 'no';
    if (typeof v === 'number') return String(Math.round(v * 1000) / 1000);
    if (typeof v === 'string') return /^\d{4}-\d\d-\d\dT/.test(v) ? new Date(v).toLocaleString([], { month: 'numeric', day: 'numeric', hour: 'numeric', minute: '2-digit', second: '2-digit' }) : v;
    if (Array.isArray(v)) return v.length ? v.map(fmt).join(v.some(function (x) { return x && typeof x === 'object'; }) ? '; ' : ', ') : 'none';
    if (typeof v === 'object') { var ks = Object.keys(v); return ks.length ? ks.map(function (k) { return k.replace(/_/g, ' ') + ' ' + fmt(v[k]); }).join(' · ') : 'none'; }
    return String(v);
  }
  function fmtKey(k, v) {
    if (k === 'door_window_sensors' && v && typeof v === 'object') {
      var names = Object.keys(v);
      return names.length ? names.map(function (n) { return (v[n].friendly_name || n) + ': ' + (v[n].open ? 'open' : 'closed'); }).join('; ') : 'none';
    }
    if (k === 'comfort_family_last_switch' && v && typeof v === 'object') {
      return (v.from || '?') + ' → ' + (v.to || '?') + ' at ' + tDay(v.ts) + (v.reason ? ' (' + v.reason + ')' : '');
    }
    return fmt(v);
  }

  // ---------- plain-language times ----------
  function when(iso) { var d = iso ? new Date(iso) : null; return d && !isNaN(d) ? d : null; }
  function tShort(d) { return d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' }); }
  function tDay(iso) {  // "7:30 AM" today, "Mon 7:30 AM" on another day
    var d = when(iso); if (!d) return DASH;
    return (d.toDateString() === new Date().toDateString() ? '' : d.toLocaleDateString([], { weekday: 'short' }) + ' ') + tShort(d);
  }
  function dayLabel(ymd) {  // "2026-11-07" (a local calendar day, not UTC midnight) -> "Sat 11/7"
    var m = /^(\d{4})-(\d\d)-(\d\d)/.exec(ymd || ''); if (!m) return DASH;
    var d = new Date(+m[1], +m[2] - 1, +m[3]);
    return d.toLocaleDateString([], { weekday: 'short' }) + ' ' + (d.getMonth() + 1) + '/' + d.getDate();
  }
  function count(n, one, many) { return n + ' ' + (n === 1 ? one : many); }
  function has(x) { return x != null && x !== ''; }

  // ---------- Prediction engines, in words (Issue #1399) ----------
  // v = v4_model_summary() (engines.v4, or automation_state.v4_model), pe = its prediction_engines block
  // (prediction_engines.engine_state()), cf = automation_state.control_feed. Temperatures and errors arrive in the user's unit (v.unit).
  function engineRows(v, pe, cf) {
    var cat = (pe && pe.engines) || [], u = (v && v.unit) || ND.sym(ND.unit), rows = [];
    function find(id) { for (var i = 0; i < cat.length; i++) if (cat[i].id === id) return cat[i]; return null; }
    function label(id) { var e = find(id); return (e && e.label) || id; }
    function conf(e) { return e.confidence && e.confidence !== 'none' ? e.confidence + ' confidence' : ''; }
    var prim = pe ? find(pe.primary) : null, sh = pe && pe.shadow && pe.shadow !== 'none' ? find(pe.shadow) : null;
    rows.push(['Primary engine', !pe || !pe.primary ? DASH : !prim ? pe.primary : prim.label + ': ' + (prim.usable ? ['usable', conf(prim)].filter(Boolean).join(', ') : 'still learning')]);
    rows.push(['Shadow engine', !pe ? DASH : !pe.shadow || pe.shadow === 'none' ? 'none' : !sh ? pe.shadow : sh.label + ': ' + (sh.usable ? conf(sh) || 'usable' : 'still learning')]);
    var dec = DASH;
    if (cf && cf.mode) {
      var pts = has(cf.path_points) ? ' (' + count(cf.path_points, 'point', 'points') + ')' : '';
      if (cf.mode === 'defaults') dec = 'default behaviour (' + (prim ? prim.label : (pe && pe.primary) || 'the main engine') + ' still learning)';
      else dec = label(cf.mode) + '’s ' + (cf.own_path ? 'own path' : 'curve') + pts;
    }
    rows.push(['Decisions use', dec]);
    v = v && !v.error ? v : {};
    var learned = DASH, lf = v.last_fit || {};
    if (v.has_model === false) learned = 'not yet';
    else if (has(v.fitted_at)) {
      learned = tDay(v.fitted_at) + (has(v.span_days) ? ' from ' + count(Math.round(v.span_days * 10) / 10, 'day', 'days') + ' of history' : '') + (has(v.n_anchors) ? ', ' + count(v.n_anchors, 'point', 'points') : '');
      if (lf.at && lf.ok !== false) learned += ' (' + ({ first: 'first fit', nightly: 'nightly refit', retry: 'retry' }[lf.reason] || String(lf.reason || 'fit').replace(/_/g, ' ')) + (has(lf.seconds) ? ', ' + lf.seconds + ' s' : '') + ')';
    }
    if (lf.at && lf.ok === false) learned = (learned === DASH || learned === 'not yet' ? '' : learned + '; ') + 'last attempt ' + tDay(lf.at) + ' failed';
    rows.push(['Last learned', learned]);
    var mae = v.mae_by_lead && typeof v.mae_by_lead === 'object' ? v.mae_by_lead : {};
    var leads = Object.keys(mae).filter(function (k) { return mae[k] != null; }).sort(function (a, b) { return parseFloat(a) - parseFloat(b); });
    rows.push(['Typical miss', leads.length ? leads.map(function (k) { return k.replace(/^(\d+)\s*h$/, '$1 h') + ' ' + Number(mae[k]).toFixed(2) + u; }).join(' · ') : DASH]);
    var sens = Array.isArray(v.sensors) ? v.sensors : [], names = v.sensor_names || {};
    var sname = function (id) { return id === '_ca' ? 'Climate Advisor reading' : names[id] || id; };
    rows.push(['Sensors modelled', !sens.length ? DASH : sens.length === 1 && sens[0] === '_ca' ? 'Climate Advisor reading only' : sens.map(sname).join(', ')]);
    var lc = v.last_cycle || {};
    rows.push(['Last prediction', !lc.at ? DASH : [tDay(lc.at), has(lc.inputs) ? count(lc.inputs, 'forecast input', 'forecast inputs') : '', has(lc.ms) ? Math.round(lc.ms) + ' ms' : '', has(lc.archived) ? count(lc.archived, 'slot', 'slots') + ' archived' : '', lc.predicted === false ? 'no prediction made' : ''].filter(Boolean).join(', ')]);
    var ar = v.archive_slots, arTxt = DASH;
    if (ar && typeof ar === 'object') {
      var vals = Object.keys(ar).map(function (k) { return Number(ar[k]) || 0; });
      var n = '_ca' in ar ? Number(ar._ca) || 0 : Math.max.apply(null, vals.concat([0]));
      var since = when(v.archive_since);
      arTxt = n ? count(n, 'slot', 'slots') + (since ? ' since ' + (since.getMonth() + 1) + '/' + since.getDate() : '') : 'empty';
    }
    rows.push(['Archive', arTxt]);
    rows.push(['Mass node', has(v.mass) ? Number(v.mass).toFixed(1) + u : DASH]);
    rows.push(['Fitting now', v.fit_running == null ? DASH : fmt(!!v.fit_running)]);
    rows.push(['Engine available', v.engine_available == null ? DASH : fmt(!!v.engine_available)]);
    return rows;
  }

  // ---------- the operation timeline ("Plan"), in words (Issue #1399): no step list ----------
  function planRows(t) {
    if (!t || t.error || !t.generated_at) return [['Plan', 'not built yet']];
    var lr = t.last_run || {}, reasons = Array.isArray(lr.reasons) ? lr.reasons.filter(Boolean) : [];
    var rows = [
      ['Generated', tDay(t.generated_at) + (t.day ? ' for ' + dayLabel(t.day) : '')],
      ['Covers through', tDay(t.horizon_end)],
      ['Steps', String((t.steps || []).length)],
      ['Last checked', !lr.at ? DASH : tDay(lr.at) + (lr.regenerated ? ', regenerated' + (reasons.length ? ' (' + reasons.join(', ').replace(/_/g, ' ') + ')' : '') : ', not regenerated')]
    ];
    if (t.regen_reason) rows.push(['Last regenerated because', String(t.regen_reason)]);
    return rows;
  }

  function buildGroups() {
    var groups = [];
    function row(k, v, hintKey) { return { k: k, v: v, hint: HINTS[hintKey || k] || '' }; }
    function rowsOf(list) { return list.map(function (r) { return row(r[0], r[1]); }); }
    GROUPS.forEach(function (g) {
      var rows = g[1].filter(function (k) { return A && k in A && !CUSTOM[k]; }).map(function (k) { return row(k, fmtKey(k, A[k])); });
      if (g[0].indexOf('Prediction engines') === 0) {
        var v4s = (E && E.v4) || (A && A.v4_model);
        rows = rows.concat(rowsOf(engineRows(v4s, v4s && v4s.prediction_engines, A && A.control_feed)));
        if (E) {
          ['k_passive', 'k_solar', 'solar_phase_offset_h', 'k_vent_window', 'k_vent_fan'].forEach(function (k) {
            var e = E[k]; if (e) rows.push(row(k, e.active ? fmt(e.value) + (e.confidence ? ' (' + e.confidence + ')' : '') : '(not yet active)'));
          });
          var h = E.k_active_hvac; if (h) rows.push(row('k_active_hvac', h.active && h.value ? 'heat ' + fmt(h.value.heat) + ' · cool ' + fmt(h.value.cool) + (h.confidence ? ' (' + h.confidence + ')' : '') : '(not yet active)'));
          rows.push(row('physics', (E.ode_version || '?') + ' · ' + (E.physics_eligible ? 'eligible' : 'not eligible') + (E.physics_eligible_reason ? ' · ' + E.physics_eligible_reason : '')));
        }
      }
      if (g[0] === 'Plan' && A && 'operation_timeline' in A) rows = rowsOf(planRows(A.operation_timeline));
      groups.push({ title: g[0] === 'Prediction engines (v4 shadow)' ? 'Prediction engines' : g[0], rows: rows });
    });
    if (A && A.classification) { groups.splice(8, 0, { title: 'Today’s classification', rows: Object.keys(A.classification).map(function (k) { return row(k, fmt(A.classification[k]), 'classification.' + k); }) }); }
    if (A && A.thermal_pipeline) {
      var tp = A.thermal_pipeline;
      groups.push({ title: 'Learning pipeline', rows: (tp.pending || []).map(function (p) {
        return row('pending ' + p.obs_type, p.status + ' · ' + p.elapsed_minutes + ' min · ' + p.sample_count + ' samples · indoor Δ' + p.indoor_delta_f + '° · outdoor ' + p.outdoor_f + '°');
      }).concat(Object.keys(tp.rejection_log_counts || {}).map(function (k) { return row('skipped ' + k, String(tp.rejection_log_counts[k])); })) });
    }
    var lc = EV && EV.diagnostics && EV.diagnostics.last_command;
    if (lc) groups.push({ title: 'Last thermostat command', rows: Object.keys(lc).map(function (k) { return row(k, fmt(lc[k])); }) });
    return groups;
  }

  function render() {
    var q = ($('dbg-search').value || '').toLowerCase(), host = $('dbg-groups'), total = 0;
    if (!A) { return; }
    var html = '';
    buildGroups().forEach(function (g) {
      var rows = g.rows.filter(function (r) { return !q || (r.k + ' ' + r.v).toLowerCase().indexOf(q) >= 0; });
      if (!rows.length) return;
      total += rows.length;
      html += '<section class="card" style="margin:0 0 10px"><h2 style="font-size:14px">' + esc(g.title) + ' <span class="sub">' + rows.length + ' fields</span></h2><table class="field-table"><tbody>' +
        rows.map(function (r) { return '<tr><td class="k">' + esc(r.k) + (r.hint ? '<span class="hint">' + esc(r.hint) + '</span>' : '') + '</td><td class="mono">' + esc(r.v) + '</td></tr>'; }).join('') + '</tbody></table></section>';
    });
    host.innerHTML = html || '<div class="empty">No field matches the filter.</div>';
    $('dbg-count').textContent = total + ' fields';
    renderEvents();
  }

  function renderEvents() {
    var q = ($('dbg-search').value || '').toLowerCase();
    var evs = ((EV && EV.events) || []).slice().reverse(), counts = {};
    evs.forEach(function (e) { counts[e.type] = (counts[e.type] || 0) + 1; });
    $('dbg-ev-types').innerHTML = Object.keys(counts).sort().map(function (k) {
      return '<button type="button" class="pill" data-type="' + esc(k) + '" aria-pressed="' + (!evOff[k]) + '">' + esc(k.replace(/_/g, ' ')) + ' <span class="n">' + counts[k] + '</span></button>';
    }).join('');
    var rows = evs.filter(function (e) { return !evOff[e.type]; }).filter(function (e) { return !q || JSON.stringify(e).toLowerCase().indexOf(q) >= 0; });
    $('dbg-ev-rows').innerHTML = rows.length ? rows.map(function (e) {
      var d = Object.assign({}, e); delete d.time; delete d.type;
      return '<tr><td style="white-space:nowrap;color:var(--ink2)">' + esc(ND.fmtTime(e.time)) + '</td><td>' + esc(String(e.type).replace(/_/g, ' ')) + '</td><td class="mono" style="color:var(--ink2);font-size:12px">' +
        esc(Object.keys(d).map(function (k) { return k + '=' + (d[k] && typeof d[k] === 'object' ? fmt(d[k]) : d[k]); }).join('  ')) + '</td></tr>';
    }).join('') : '<tr><td colspan="3" class="empty">' + (EV ? 'No events in this window.' : 'Event log unavailable.') + '</td></tr>';
    $('dbg-ev-count').textContent = 'last ' + $('dbg-ev-hours').selectedOptions[0].textContent + ' · ' + rows.length + ' of ' + evs.length;
    if (EV && EV.is_truncated) $('dbg-ev-count').textContent += ' · older events were not kept';
  }

  async function loadEvents() {
    try { EV = await ND.api('event_log?hours=' + $('dbg-ev-hours').value); } catch (e) { EV = null; }
    render();
  }
  async function load() {
    var r = await Promise.allSettled([ND.api('automation_state'), ND.api('engines'), ND.api('event_log?hours=' + $('dbg-ev-hours').value)]);
    A = r[0].status === 'fulfilled' ? r[0].value : null; E = r[1].status === 'fulfilled' ? r[1].value : null; EV = r[2].status === 'fulfilled' ? r[2].value : null;
    loaded = true;
    if (!A) $('dbg-groups').innerHTML = '<div class="err">Failed to load: ' + esc(r[0].reason ? r[0].reason.message : 'no data') + '</div>';
    else render();
    loadBacktest();
  }

  // ---------- downloads (same bundle as classic's Download Logs) ----------
  async function downloadLogs() {
    var b = $('dbg-dl-logs'), orig = b.textContent; b.textContent = 'Downloading...'; b.disabled = true;
    try {
      var r = await Promise.allSettled([ND.api('event_log?hours=168'), ND.api('investigation_reports')]);
      var bundle = { downloaded_at: new Date().toISOString(), events: r[0].status === 'fulfilled' ? (r[0].value.events || []) : [], diagnostics: r[0].status === 'fulfilled' ? (r[0].value.diagnostics || null) : null };
      if (r[1].status === 'fulfilled') bundle.investigation_reports = r[1].value;
      ND.download(JSON.stringify(bundle, null, 2), 'climate-advisor-logs-' + new Date().toISOString().slice(0, 10) + '.json', 'application/json');
    } catch (e) { ND.toast('Failed to download logs: ' + e.message, 'err'); } finally { b.textContent = orig; b.disabled = false; }
  }
  async function downloadDiagnostics() {
    try {
      var ev = await ND.api('event_log?hours=2');
      ND.download(JSON.stringify({ downloaded_at: new Date().toISOString(), diagnostics: ev.diagnostics || null, automation_state: A }, null, 2), 'climate-advisor-diagnostics-' + new Date().toISOString().slice(0, 10) + '.json', 'application/json');
    } catch (e) { ND.toast('Failed to download diagnostics: ' + e.message, 'err'); }
  }

  // ---------- backtest lab ----------
  async function loadBacktest(lead, range) {
    var host = $('dbg-bt');
    lead = lead || (host.getAttribute('data-lead') || '4h'); range = range || (host.getAttribute('data-range') || '7d');
    host.setAttribute('data-lead', lead); host.setAttribute('data-range', range);
    var opt = function (list, cur) { return list.map(function (x) { return '<option' + (x === cur ? ' selected' : '') + '>' + x + '</option>'; }).join(''); };
    var head = '<div class="row"><label class="small">Look-ahead <select id="bt-lead" class="ctl">' + opt(['1h', '4h', '12h', '24h'], lead) + '</select></label>' +
      '<label class="small">Range <select id="bt-range" class="ctl">' + opt(['24h', '3d', '7d', '30d'], range) + '</select></label><button type="button" class="btn small" id="bt-run">Run</button></div>';
    try {
      var d = await ND.api('prediction_accuracy?lead=' + lead + '&range=' + range), s = d.stats || {}, u = d.unit || ND.sym(ND.unit), g = d.gate_summary || {}, m = d.model || {};
      var tile = function (l, v) { return '<div class="tile"><div class="l">' + esc(l) + '</div><div class="v">' + esc(v) + '</div></div>'; };
      var f = function (x) { return x == null ? '—' : Number(x).toFixed(2) + ' ' + u; };
      host.innerHTML = head + '<div class="note" style="margin:8px 0">' + (g.scored != null ? 'v4 beats both v3 and "no change" in ' + g.passed + ' of ' + g.scored + ' scored backtest rows (' + g.total + ' rows in all).' : 'The backtest has not been scored yet.') + ' Main prediction on Status: ' + esc(d.primary_engine || 'v4') + '.' +
        ' Model: ' + esc(m.confidence || 'n/a') + (m.span_days != null ? ', ' + m.span_days + ' days of data' : '') + (m.fitted_at ? ', fitted ' + esc(ND.fmtDateTime(m.fitted_at)) : '') + '.</div>' +
        '<div class="tiles">' + tile('Points scored', s.n != null ? String(s.n) : '—') + (d.primary_engine !== 'v3' ? tile('v4 average miss', f(s.v4_mae)) + tile('v3 average miss', f(s.v3_mae)) : tile('v3 average miss', f(s.v3_mae)) + tile('v4 average miss', f(s.v4_mae))) + tile('"No change" miss', f(s.flat_mae)) + (d.primary_engine !== 'v3' ? tile('v4 bias', f(s.v4_bias)) + tile('v3 bias', f(s.v3_bias)) : tile('v3 bias', f(s.v3_bias)) + tile('v4 bias', f(s.v4_bias))) + '</div>';
    } catch (e) { host.innerHTML = head + '<div class="empty" style="margin-top:8px">Backtest data is not available yet (' + esc(e.message) + ').</div>'; }
  }

  function init() {
    $('dbg-search').addEventListener('input', render);
    $('dbg-dl-logs').addEventListener('click', downloadLogs);
    $('dbg-dl-diag').addEventListener('click', downloadDiagnostics);
    $('dbg-ev-hours').addEventListener('change', loadEvents);
    $('dbg-ev-all').addEventListener('click', function () { evOff = {}; renderEvents(); });
    $('dbg-ev-none').addEventListener('click', function () { ((EV && EV.events) || []).forEach(function (e) { evOff[e.type] = true; }); renderEvents(); });
    $('dbg-ev-types').addEventListener('click', function (e) {
      var b = e.target.closest('button[data-type]'); if (!b) return;
      var k = b.getAttribute('data-type'); evOff[k] = !evOff[k]; renderEvents();
      var again = $('dbg-ev-types').querySelector('[data-type="' + k + '"]'); if (again) again.focus();
    });
    $('dbg-bt').addEventListener('click', function (e) { if (e.target.id === 'bt-run') loadBacktest($('bt-lead').value, $('bt-range').value); });
    $('dbg-bt-link').addEventListener('click', function () { var z = ND.zone; if (z) this.href = 'prediction_accuracy.html?zone=' + encodeURIComponent(z); });
  }

  function startTimer() { stopTimer(); timer = setInterval(function () { if (ND.active === 'debug' && !document.hidden) load(); }, 60000); }
  function stopTimer() { if (timer) clearInterval(timer); timer = null; }

  ND.register('debug', { show: function () { load(); startTimer(); }, refresh: load });
  ND.on('init', init);
  ND.on('zone', function () { loaded = false; if (ND.active === 'debug') load(); });
  ND.on('refresh', function () { if (ND.active === 'debug') load(); });
  ND.on('state-changed', function () { if (ND.active === 'debug') load(); });
  ND.GROUPS = GROUPS;
  ND.DEBUG_UNGROUPED = { nested: NESTED, moved: MOVED, downloadOnly: DOWNLOAD_ONLY };
})();
