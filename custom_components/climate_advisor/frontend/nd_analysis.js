/* Issue #1230 R5: Analysis tab - is it working, and what can improve?
   Reads only existing endpoints: learning, engines, event_log (diagnostics.control_quality), chart_data
   (thermal_model), ai_status, investigation_reports. Writes only what classic writes: respond_suggestion,
   activity_record / ai_investigate (report generation), delete_report, submit_github_issue. */
(function () {
  'use strict';
  var $ = ND.$, esc = ND.esc;
  // Issue #1341: every sentence below is a catalog message (area dashboard, keys dashboard.analysis.*). T() renders it
  // in the viewer's language; numbers are passed as numbers so ICU formats them. Names that are Latin text (engine ids,
  // models, error text from the backend) are isolated with U+2068..U+2069 in RTL text, as the Python renderer does.
  function T(key, params) { return CAI18N.t('dashboard.analysis.' + key, params); }
  var iso = CAI18N.iso;
  // locale number with fixed decimals, no grouping (the old toFixed output in English); null/NaN -> the dash
  function nf(v, d) {
    if (v == null || isNaN(v)) return '\u2014';
    d = d == null ? 1 : d;
    return CAI18N.fmt.number(Number(v), { minimumFractionDigits: d, maximumFractionDigits: d, useGrouping: false });
  }
  function ni(v) { return CAI18N.fmt.number(Number(v), { maximumFractionDigits: 0, useGrouping: false }); }
  var D = {};              // last loaded payloads
  var currentReport = null, activityTable = null;
  var allHistory = [], filtered = [], histFilter = 'all', histShowing = 5, aiDisabled = false;
  var loaded = false, loadSeq = 0;

  // Reason codes (learning_health rejections) and observation types: the English is in the catalog; an unknown code shows itself.
  var REASON_CODES = ["indoor_unavailable", "insufficient_samples", "window_elapsed_too_few_samples", "insufficient_decay", "ols_bad_fit", "poor_fit", "state_changed", "outlier", "abandoned", "cycle_too_short", "cycle_signal_below_resolution", "cycle_excluded_window_fan", "cycle_setpoint_change", "cycle_gap_restart", "cycle_thermostat_unavailable", "cycle_no_off_edge", "cycle_sensor_swap", "hvac_run_too_short", "hvac_signal_below_resolution", "no_k_passive", "new_session_started", "fan_activated", "hvac_mode_switch", "hvac_unreported", "ols_bounds"];
  var OBS_KEYS = ["passive_decay", "vent_window_decay", "vent_fan_decay", "solar_gain", "hvac_heat", "hvac_cool", "fan_only_decay", "ventilated_decay"];
  function reasonText(code) { return REASON_CODES.indexOf(code) >= 0 ? T('reason.' + code) : code; }
  // SECTIONS keep the English label (the downloaded Markdown / GitHub issue text stays English, like the ## headers the
  // parser needs); the screen shows T('section.' + key).
  var SECTIONS = [
    { key: 'activity_summary', label: 'Activity summary' },
    { key: 'incongruities', label: 'Incongruities found' },
    { key: 'data_quality', label: 'Data quality issues' },
    { key: 'errors_warnings', label: 'System errors / warnings' },
    { key: 'hypotheses', label: 'Hypotheses' },
    { key: 'recommended_actions', label: 'Recommended actions' },
    { key: 'assumptions', label: 'Assumptions & confidence' }
  ];
  var HEADER_MAP = {
    'ACTIVITY SUMMARY': 'activity_summary', 'INCONGRUITIES FOUND': 'incongruities', 'DATA QUALITY ISSUES': 'data_quality',
    'SYSTEM ERRORS / WARNINGS': 'errors_warnings', 'HYPOTHESES': 'hypotheses', 'RECOMMENDED ACTIONS': 'recommended_actions',
    'ASSUMPTIONS & CONFIDENCE': 'assumptions'
  };

  function pct(v) { return v == null ? '—' : CAI18N.fmt.number(Math.round(v * 100) / 100, { style: 'percent', maximumFractionDigits: 0 }); }
  // "HH:MM:SS" schedule strings from the API (24 h notation, as before): the digits are locale digits, no AM/PM
  function hm(s) {
    var mm = /^(\d+):(\d\d)/.exec(s || ''); if (!mm) return '—';
    return ni(+mm[1]) + ':' + CAI18N.fmt.number(+mm[2], { minimumIntegerDigits: 2, useGrouping: false });
  }
  function yn(v) { return T(v ? 'yes' : 'no'); }
  function span(a, b) { return a ? ND.fmtTime(a) + '–' + ND.fmtTime(b) : '—'; }
  function r0(v) { return v == null ? '—' : ni(v); }
  function tdisp(f, d) { return f == null ? '—' : nf(ND.fToDisp(f), d == null ? 0 : d); }
  function err(el, msg) { el.innerHTML = '<div class="err">' + esc(T('load_failed', { error: msg })) + '</div>'; }
  function mins(n) { return T('minutes', { n: Math.round(n) }); }

  // ---------- load ----------
  async function load() {
    var seq = ++loadSeq;
    var calls = await Promise.allSettled([
      ND.api('learning'), ND.api('engines'), ND.api('event_log?hours=2'), ND.api('chart_data?range=6h'),
      ND.api('ai_status'), ND.api('investigation_reports'),
      // Issue #1475: the headline 4 h error is the PRIMARY engine's live miss (the control-quality archive is v3's log)
      ND.api('prediction_accuracy?lead=4h&range=24h')
    ]);
    if (seq !== loadSeq) return; // a newer load (zone switch) owns the screen
    var v = function (i) { return calls[i].status === 'fulfilled' ? calls[i].value : null; };
    var e = function (i) { return calls[i].status === 'rejected' ? calls[i].reason.message : null; };
    D.learning = v(0); D.engines = v(1); D.events = v(2); D.chart = v(3); D.ai = v(4); D.reports = v(5); D.acc = v(6);
    D.errs = [e(0), e(1), e(2), e(3), e(4), e(5), e(6)];
    loaded = true;
    renderAll();
  }

  function renderAll() {
    renderKpis(); renderDays(); renderModel(); renderControl(); renderSuggestions(); renderAiLine(); loadHistory();
  }

  // ---------- headline numbers ----------
  function cq24() {
    var cq = D.events && D.events.diagnostics && D.events.diagnostics.control_quality;
    return cq && cq.windows && cq.windows['24h'] ? cq.windows['24h'] : null;
  }
  function renderKpis() {
    var L = D.learning, host = $('an-kpis');
    if (!L) { host.innerHTML = '<div class="tile"><div class="l">' + esc(T('kpi.headline')) + '</div><div class="err">' + esc(D.errs[0] || T('kpi.no_data')) + '</div></div>'; return; }
    var c = L.compliance || {}, active = c.status === 'active';
    var ps = ND.status && ND.status.promise_summary;
    var today = L.today_record ? L.today_record.manual_overrides || 0 : 0;
    // Issue #1475: every score is over the most recent window_days records (14), not every recorded day
    var wd = c.window_days || Math.min(14, c.days_recorded || 0);
    var pe = primaryMiss(), lastHit = promiseLastHit(ps);
    var tiles = [
      { l: T('kpi.comfort_score'), v: active ? pct(c.comfort_score) : '—', n: T('kpi.comfort_note', { wd: wd, recorded: c.days_recorded || 0 }),
        t: T('kpi.comfort_tip', { wd: wd }) },
      { l: T('kpi.window_advice'), v: active ? pct(c.window_compliance) : '—', n: c.window_compliance_denominator != null ? T('kpi.window_note', { n: c.window_compliance_denominator, wd: wd }) : '' },
      { l: T('kpi.runtime'), v: active ? mins(c.avg_daily_hvac_runtime_minutes || 0) : '—',
        n: active && c.avg_daily_thermostat_fan_only_runtime_minutes ? T('kpi.runtime_note_fan', { wd: wd, fan: Math.round(c.avg_daily_thermostat_fan_only_runtime_minutes) }) : T('kpi.runtime_note', { wd: wd }) },
      { l: T('kpi.manual'), v: active ? ni(c.total_manual_overrides || 0) : '—', n: T('kpi.manual_note', { wd: wd, today: today }) },
      // Issue #1490: misses over the last 30 days only (older misses age out), not a lifetime total
      { l: T('kpi.promises'), v: ps ? ni(ps.total_hits || 0) : '0',
        n: ps && ps.total_hits ? (lastHit ? T('kpi.promises_note_last', { when: ND.fmtDateTime(lastHit) }) : T('kpi.promises_note')) : T('kpi.promises_none'),
        t: T('kpi.promises_tip') },
      { l: T('kpi.pred_error'), v: pe ? nf(pe.mae, 1) + '°' : '—',
        n: pe ? T('kpi.pred_note', { engine: iso(pe.engine), bias: (pe.bias > 0 ? '+' : '') + nf(pe.bias, 1), dir: pe.bias > 0 ? 'warm' : 'cool' }) : T('kpi.pred_none'),
        t: T('kpi.pred_tip') }
    ];
    host.innerHTML = tiles.map(function (t) {
      return '<div class="tile"' + (t.t ? ' title="' + esc(t.t) + '"' : '') + '><div class="l">' + esc(t.l) + '</div><div class="v">' + esc(t.v) + '</div><div class="n">' + esc(t.n) + '</div></div>';
    }).join('');
  }

  // The primary engine's live 4 h miss over the last 24 h (prediction_accuracy, already in the user's unit), or null
  function primaryMiss() {
    var a = D.acc, s = a && a.stats; if (!s) return null;
    var eng = a.primary_engine || 'v4', mae = s[eng + '_mae'], bias = s[eng + '_bias'];
    return mae == null ? null : { engine: eng, mae: mae, bias: bias || 0 };
  }
  function promiseLastHit(ps) {
    var t = ((ps && ps.promises) || []).map(function (p) { return p.last_hit; }).filter(Boolean).sort();
    return t.length ? t[t.length - 1] : null;
  }

  // ---------- days table: yesterday / today / tomorrow (plan) ----------
  function renderDays() {
    var L = D.learning, host = $('an-days');
    if (!L) { err(host, D.errs[0] || T('no_data')); return; }
    var y = L.yesterday_record, t = L.today_record, n = L.tomorrow_plan, u = ND.sym(L.unit || ND.unit), TL = L.today_live || {};
    var lo = L.comfort_range_low, hi = L.comfort_range_high;
    var tip = T('days.tip', { range: lo != null && hi != null ? 'yes' : 'no', lo: nf(lo, 0), hi: nf(hi, 0), unit: u });
    function pair(rec, a, b) { return tdisp(rec[a]) + (CAI18N.dir === 'rtl' ? '←' : '→') + tdisp(rec[b]); } // forecast → observed: the arrow points the reading direction
    function hl(rec) {
      // Issue #1475: today's observed extremes fill only at end of day; show the outdoor high/low seen so far
      if (rec === t && TL.observed_high_f != null && rec.observed_high_f == null) {
        return T('days.hl_so_far', { fh: tdisp(rec.forecast_high_f), fl: tdisp(rec.forecast_low_f), oh: tdisp(TL.observed_high_f), ol: tdisp(TL.observed_low_f) });
      }
      return pair(rec, 'forecast_high_f', 'observed_high_f') + ' / ' + pair(rec, 'forecast_low_f', 'observed_low_f');
    }
    function rec(fn) { return function (r) { return r ? fn(r) : '—'; }; }
    function fixedTimes(r) { return T('days.fixed_times', { open: hm(r.window_open_time), close: hm(r.window_close_time), day_type: r.day_type ? CAI18N.word('dashboard.analysis.day_type.', r.day_type) : '?' }); }
    // Issue #1475: a record's flag means "recommended at any point that day" (compliance credit); today also says
    // what holds now, and names the plan's own periods instead of the day type's fixed times
    function advised(r) {
      if (r !== t || TL.windows_recommended_now == null) return r.windows_recommended ? T('days.adv_yes_fixed', { times: fixedTimes(r) }) : yn(false);
      var now = TL.windows_recommended_now, plan = TL.plan_windows || [];
      var head = now ? 'now' : r.windows_recommended ? (TL.forecast_windows_recommended ? 'earlier_live' : 'earlier') : 'none';
      var planned = plan.map(function (w) { return hm(w[0]) + '–' + hm(w[1]); }).join(', ');
      var how = plan.length ? 'plan' : (now ? 'fixed' : 'none');
      return T('days.adv_today', { head: head, how: how, plan: planned, times: how === 'fixed' ? fixedTimes(r) : '' });
    }
    function opened(r) {
      if (!r.windows_physically_opened && !r.window_physical_open_time) return yn(false);
      return T('days.opened', { span: span(r.window_physical_open_time, r.window_physical_close_time), credit: r.windows_recommended ? (r.windows_opened ? 'counted' : 'not_counted') : 'none' });
    }
    function runtime(r) {
      var tot = r.hvac_runtime_minutes || 0, h = r.hvac_heat_runtime_minutes, c = r.hvac_cool_runtime_minutes;
      if (h == null && c == null) return mins(tot);
      // the split is recorded from 0.9.x on; a day that began on an older version has minutes only in the total
      return h + c >= tot - 1 ? T('days.runtime_split', { tot: Math.round(tot), heat: Math.round(h), cool: Math.round(c) }) : T('days.runtime_nosplit', { tot: Math.round(tot) });
    }
    var rows = [
      [T('days.row.day_type'), rec(function (r) { return CAI18N.word('dashboard.analysis.day_type.', r.day_type || '—') + ' · ' + CAI18N.word('dashboard.analysis.trend.', r.trend_direction || '—'); }),
        function (p) { return p ? CAI18N.word('dashboard.analysis.day_type.', p.day_type || '—') : ''; }],
      [T('days.row.hvac_mode'), rec(function (r) { return CAI18N.word('common.hvac_mode_word.', r.hvac_mode_recommended || '—'); }), function (p) { return p ? CAI18N.word('common.hvac_mode_word.', p.hvac_mode || '—') : ''; },
        T('days.tip.hvac_mode')],
      [T('days.row.high_low'), rec(hl),
        function (p) { return p && (p.expected_high != null || p.expected_low != null) ? T('days.expected', { hi: nf(p.expected_high, 0) + u, lo: nf(p.expected_low, 0) + u }) : ''; }],
      [T('days.row.windows_advised'), rec(advised),
        function (p) { return p ? (p.windows_recommended ? T('days.adv_plan', { open: hm(p.window_open_time), close: hm(p.window_close_time) }) : yn(false)) : ''; },
        T('days.tip.windows_advised')],
      [T('days.row.windows_open'), rec(opened), function () { return ''; },
        T('days.tip.windows_open')],
      [T('days.row.hvac_runtime'), rec(runtime), function () { return ''; }],
      [T('days.row.fan_runtime'), rec(function (r) { return r.thermostat_fan_only_runtime_minutes == null ? T('days.dash_min') : mins(r.thermostat_fan_only_runtime_minutes); }), function () { return ''; }],
      [T('days.row.avg_indoor'), rec(function (r) {
        var m = r.comfort_violations_minutes || 0;
        var avg = r.avg_indoor_temp != null ? tdisp(r.avg_indoor_temp, 1) + u : r === t && TL.avg_indoor_f != null ? T('days.so_far', { v: tdisp(TL.avg_indoor_f, 1) + u }) : '—';
        return T('days.outside_comfort', { avg: avg, min: Math.round(m), pct: Math.round(m / 1440 * 100) });
      }), function () { return ''; }, tip],
      [T('days.row.night_setback'), rec(function (r) {
        if (r.setback_depth_f) return T('days.setback', { depth: nf(ND.dFToDisp(r.setback_depth_f), 1) + u, adaptive: r.setback_was_adaptive ? 'yes' : 'no' });
        if (r.setback_skipped_reason) return T('days.setback_skipped', { reason: CAI18N.word('dashboard.analysis.days.skip_reason.', r.setback_skipped_reason, String(r.setback_skipped_reason).replace(/_/g, ' ')) });
        // Issue #1474: bedtime results were not recorded before 0.9.16
        return r === t ? T('days.after_bedtime') : T('days.not_recorded');
      }), function () { return ''; }],
      [T('days.row.precond'), function (r) { return r && r.pre_condition != null ? yn(r.pre_condition) : r ? T('days.not_recorded') : '—'; }, function (p) { return p ? yn(p.pre_condition) : ''; },
        T('days.tip.precond')],
      [T('days.row.manual_pauses'), rec(function (r) { return T('days.manual_pauses', { manual: r.manual_overrides || 0, pauses: r.door_window_pause_events || 0 }); }), function () { return ''; }],
      [T('days.row.away'), rec(function (r) { return r0(r.occupancy_away_minutes); }), function () { return ''; }],
      // Filled at end of day from the optional Settings > Advanced energy rate and heating/cooling power (#1230 R5c);
      // blank ("—") when any is unset. Tomorrow stays blank: the plan carries no predicted HVAC minutes yet.
      [T('days.row.cost'), rec(function (r) {
        if (r.estimated_cost != null) return nf(r.estimated_cost, 2);
        if (!L.cost_configured) return T('days.cost_set_rate');
        return r === t ? T('days.cost_after_day') : '—';
      }), function () { return ''; },
        T('days.tip.cost')]
    ];
    // the record's local calendar day (a 'YYYY-MM-DD' string): shown as a locale date, built from its parts (not UTC midnight)
    var dt = function (r, p) {
      var mm = r && r.date ? /^(\d{4})-(\d\d)-(\d\d)/.exec(String(r.date)) : null;
      return mm ? ' ' + CAI18N.fmt.calendarDay(+mm[1], +mm[2], +mm[3], CAI18N.lang) : p || '';
    };
    var h = '<table style="min-width:640px"><thead><tr><th scope="col"><span class="sr-only">' + esc(T('days.col.measure')) + '</span></th><th scope="col">' + esc(T('days.col.yesterday')) + esc(dt(y)) +
      '</th><th scope="col">' + esc(T('days.col.today')) + esc(dt(t)) + '</th><th scope="col">' + esc(T('days.col.tomorrow')) + esc(dt(n)) + '</th></tr></thead><tbody>';
    rows.forEach(function (r) {
      h += '<tr><th scope="row" class="k" style="font-weight:400"' + (r[3] ? ' title="' + esc(r[3]) + '"' : '') + '>' + esc(r[0]) + '</th><td class="num">' + esc(r[1](y)) +
        '</td><td class="num">' + esc(r[1](t) === '—' && !t ? T('days.no_record_today') : r[1](t)) + '</td><td class="num">' + esc(r[2](n)) + '</td></tr>';
    });
    host.innerHTML = h + '</tbody></table>';
    $('an-days-sub').textContent = T('days.sub');
    var c = L.compliance || {};
    $('an-compliance').textContent = c.status === 'active' ? T('days.compliance', { wd: c.window_days || Math.min(14, c.days_recorded), recorded: c.days_recorded, window: pct(c.window_compliance),
      hvac: Math.round(c.avg_daily_hvac_runtime_minutes || 0), fan: Math.round(c.avg_daily_thermostat_fan_only_runtime_minutes || 0), comfort: pct(c.comfort_score), overrides: c.total_manual_overrides || 0 }) : T('days.compliance_wait');
  }

  // Issues #1512/#1516: the cycle model's rows (engines payload ``decisions`` entries with source "cycle"), by mode.
  // A row carries its fields either on itself or in an object ``value``; mode comes from ``mode`` or the key.
  // q and sd_q arrive in the user's unit like every decisions row; coast_area_f is a Fahrenheit delta, converted here.
  function gradeWord(g) { return T('grade.' + (['none', 'low', 'medium', 'high'].indexOf(g) >= 0 ? g : 'none')); }
  function cycleRows(list) {
    var out = {};
    (list || []).forEach(function (r) {
      if (!r || r.source !== 'cycle') return;
      var f = r.value && typeof r.value === 'object' ? r.value : r, m = r.mode || (String(r.key || '').indexOf('cool') >= 0 ? 'cool' : 'heat');
      out[m] = f;
    });
    return out;
  }

  // ---------- the house model in plain language ----------
  function renderModel() {
    var E = D.engines, tm = D.chart && D.chart.thermal_model, host = $('an-model');
    if (!E && !tm) { err(host, D.errs[1] || T('no_data')); return; }
    var rows = [], eu = E ? ND.sym(E.unit) : ND.sym(ND.unit);
    function row(plain, raw, conf) { rows.push('<div class="kv"><div>' + esc(plain) + '</div><div class="conf">' + esc(conf || '') + '</div><div class="raw">' + esc(raw) + '</div></div>'); }
    if (E) {
      var dr = {};
      (E.decisions || []).forEach(function (r) { if (r && typeof r.value === 'number') dr[r.key] = r; });
      var kp = E.k_passive && E.k_passive.active ? E.k_passive : null, hv = E.k_active_hvac && E.k_active_hvac.active ? E.k_active_hvac : null;
      var ks = E.k_solar && E.k_solar.active ? E.k_solar : null, kw = E.k_vent_window && E.k_vent_window.active ? E.k_vent_window : null, kf = E.k_vent_fan && E.k_vent_fan.active ? E.k_vent_fan : null;
      // The raw column ("k_passive 0.0123 /h ...") names model fields, so it stays English like Debug's field names.
      var calib = function (c) { return T('model.calibration', { value: iso(CAI18N.word('dashboard.analysis.level.', c || 'na')) }); };
      if (kp) row(T('model.holds_heat', { how: Math.abs(kp.value) < 0.05 ? 'well' : 'moderately', hours: Math.round(Math.log(2) / Math.abs(kp.value)) }),
        'k_passive ' + Number(kp.value).toFixed(4) + ' /h · time constant ' + Math.round(1 / Math.abs(kp.value)) + ' h', calib(kp.confidence));
      if (hv && hv.value) row(T('model.hvac_rates', { heat: nf(hv.value.heat, 1) + eu, cool: nf(Math.abs(hv.value.cool), 1) + eu }),
        'k_active heat ' + Number(hv.value.heat).toFixed(2) + ' · cool ' + Number(hv.value.cool).toFixed(2) + ' ' + eu + '/h', calib(hv.confidence));
      // Issues #1512/#1516: the cycle model's measured heating and cooling rate, how sure it is, and the coast after a run.
      // Rows come from the engines payload (``decisions`` rows with source "cycle"); on a v4 zone decisions keep v4's rate.
      var cyc = cycleRows(E.decisions);
      // owner decision 2026-11-13: a thermostat that never reports heating/cooling: no runtime, no rates measured
      // r.text is a backend sentence (notice); it arrives in the backend language
      (E.decisions || []).forEach(function (r) { if (r && r.source === 'notice' && r.text) row(r.text, 'rate in use: ' + (r.rate_in_use || 'n/a'), ''); });
      ['heat', 'cool'].forEach(function (m) {
        var c = cyc[m]; if (!c || typeof c.q !== 'number') return;
        var cd = dr['k_active_' + m], onV4 = cd && cd.source && cd.source !== 'v3' && cd.source !== 'cycle' && typeof cd.value === 'number';
        var hasCoast = typeof c.coast_area_f === 'number' && Math.abs(c.coast_area_f) >= 0.05;
        // one whole sentence: the optional coast and v4 clauses are variants of the same message
        var line = T('model.cycle_line', { mode: m, q: nf(Math.abs(c.q), 1) + eu, grade: gradeWord(c.grade || 'none'),
          coast: hasCoast ? (c.coast_area_f * c.q > 0 ? 'keeps' : 'gives_back') : 'none', amount: hasCoast ? nf(Math.abs(ND.dFToDisp(c.coast_area_f)), 1) + eu : '',
          v4: onV4 ? 'yes' : 'no', source: onV4 ? iso(cd.source) : '', used: onV4 ? nf(Math.abs(cd.value), 1) + eu : '' });
        row(line, 'cycle q ' + Number(c.q).toFixed(2) + (typeof c.sd_q === 'number' ? ' \u00b1 ' + Number(c.sd_q).toFixed(2) : '') + ' ' + eu + '/h \u00b7 coast ' +
          (typeof c.coast_area_f === 'number' ? Number(ND.dFToDisp(c.coast_area_f)).toFixed(2) : '\u2014') + ' ' + eu + ' \u00b7 ' + (c.ne != null ? c.ne : 0) + ' informative cycles', T('model.sure', { grade: iso(CAI18N.word('dashboard.analysis.level.', c.grade || 'none')) }));
        // Issue #1512 reopened: a whole- or half-degree sensor cannot learn the rate from normal cycles; say so plainly.
        if (c.coarse_sensor && c.coarse_sensor.text) row(c.coarse_sensor.text, 'sensor reports ' + (c.coarse_sensor.label || 'coarse steps') + ' \u00b7 rate in use: ' + (c.coarse_sensor.rate_in_use || 'n/a'), '');
      });
      if (ks) {
        var ph = E.solar_phase_offset_h && E.solar_phase_offset_h.active ? E.solar_phase_offset_h.value : 0;
        // solar noon is 13:00 plus the learned phase offset; formatted as a locale time (12/24 h per the viewer's HA setting)
        var peak = CAI18N.fmt.time(new Date(2000, 0, 1, 13 + Math.floor(ph), Math.round((ph % 1) * 60)));
        row(T('model.sun', { gain: nf(ks.value, 1) + eu, peak: peak }), 'k_solar ' + Number(ks.value).toFixed(3) + ' · phase +' + Number(ph).toFixed(2) + ' h', calib(ks.confidence));
      }
      // Issue #1505: the window and fan line uses the rates the decisions and the timeline read (``decisions``: v4's own
      // on a v4 zone, v3's learner otherwise), all three from that one source so the ratios compare like with like.
      var dp = dr.k_passive || (kp ? { value: kp.value, source: 'v3' } : null);
      var dw = dr.k_vent_window || (kw ? { value: kw.value, source: 'v3' } : null), df = dr.k_vent_fan || (kf ? { value: kf.value, source: 'v3' } : null);
      if (dp && dp.value && (dw || df)) {
        var x = function (r) { return nf(Math.abs(r.value) / Math.abs(dp.value), 1) + '×'; };
        row(T('model.vent', { windows: dw ? 'yes' : 'no', fan: df ? 'yes' : 'no', wx: dw ? x(dw) : '', fx: df ? x(df) : '' }),
          (dw ? 'k_vent_window ' + Number(dw.value).toFixed(3) : 'k_vent_window not learned') + ' · ' + (df ? 'k_vent_fan ' + Number(df.value).toFixed(3) : 'k_vent_fan not learned') +
          ' · closed ' + Number(dp.value).toFixed(3) + ' /h', T('model.from', { source: iso((dw || df).source) }));
      }
      var s = $('an-model-status');
      s.textContent = T(E.physics_eligible ? 'model.status_physics' : 'model.status_fallback');
    }
    if (tm) {
      var sh = tm.swing_heat != null ? tm.swing_heat : null, sc = tm.swing_cool != null ? tm.swing_cool : null;
      var swSym = ND.sym(tm.unit || ND.unit), swDec = swSym === '°C' ? 2 : 1;  // Issue #1477: 1.5 F = 0.83 C, not 0.8
      // Issue #1477 owner decision: the rounded-up swing (detectors) with the measured one (the bedroom loop) beside it
      var swTxt = function (v, raw, measured) {
        var s = nf(v != null ? v : 1.5, swDec) + ' ' + swSym;
        return measured && raw != null && Math.abs(raw - v) > 0.004 ? T('model.swing_measured', { value: s, raw: nf(raw, 2) + ' ' + swSym }) : s;
      };
      if (sh != null || sc != null) row(T('model.swing', { heat: swTxt(sh, tm.swing_heat_raw, tm.swing_heat_measured), cool: swTxt(sc, tm.swing_cool_raw, tm.swing_cool_measured),
        est: tm.swing_heat_measured && tm.swing_cool_measured ? 'no' : 'yes' }), 'swing_heat ' + nf(sh, 2) + ' · swing_cool ' + nf(sc, 2) + ' ' + ND.sym(tm.unit || ND.unit), '');
      var envN = (tm.observation_count_passive || 0) + (tm.observation_count_vent_window || 0) + (tm.observation_count_vent_fan || 0) + (tm.observation_count_solar || 0);
      var hvN = (tm.observation_count_heat || 0) + (tm.observation_count_cool || 0);
      // Issue #1512: hvN is the evidence behind the heating/cooling rates (older measurements give way to new cycles,
      // never to time alone), not a running total
      row(T('model.kept', { house: envN, hvac: hvN }), T('model.calibration_split', { house: iso(CAI18N.word('dashboard.analysis.level.', tm.confidence_k_passive || 'none')), hvac: iso(CAI18N.word('dashboard.analysis.level.', tm.confidence || 'none')) }),
        tm.last_observation_date ? T('model.last_obs', { date: CAI18N.fmt.dateYear(new Date(tm.last_observation_date)) }) : T('model.none_yet'));
    }
    if (!rows.length) rows.push('<div class="empty">' + esc(T('model.nothing')) + '</div>');
    var health = tm && tm.learning_health, html = rows.join('');
    if (health) {
      var tot = 0, lines = [];
      OBS_KEYS.forEach(function (k) {
        var h = health[k]; if (!h || !h.attempts) return;
        var rej = Object.keys(h.rejections || {}).reduce(function (a, c) { return a + h.rejections[c]; }, 0); tot += rej;
        var last = h.last_rejection && h.last_rejection.reason_code ? reasonText(h.last_rejection.reason_code) : '';
        lines.push('<div style="display:flex;justify-content:space-between;gap:8px"><span>' + esc(T('obs.' + k)) + '</span><span>' + esc(T('model.obs_line', { kept: h.committed, skipped: rej, has_last: last ? 'yes' : 'no', last: last })) + '</span></div>');
      });
      if (lines.length) html += '<details><summary>' + esc(T('model.skipped_by_type', { n: tot })) + '</summary><div class="small" style="display:grid;gap:4px;margin-top:6px">' + lines.join('') + '</div></details>';
    }
    host.innerHTML = html;
  }

  // ---------- control quality, prediction error, promises ----------
  function renderControl() {
    var q = cq24(), host = $('an-cq'), pe = $('an-pe');
    if (!q) {
      host.innerHTML = '<div class="empty">' + (D.errs[2] ? esc(D.errs[2]) : esc(T('cq.not_enough'))) + '</div>'; pe.innerHTML = '<div class="empty">' + esc(T('cq.not_available')) + '</div>';
    } else {
      var hr = q.runtime.heating, cl = q.runtime.cooling;
      var items = [
        [T('cq.heating'), T('cq.heating_v', { min: Math.round(hr.minutes), runs: hr.runs })],
        [T('cq.median'), T('cq.median_v', { med: hr.median_run_minutes != null ? 'yes' : 'no', n: hr.median_run_minutes, short: hr.short_runs_lt_10m })],
        [T('cq.cooling'), mins(cl.minutes)], [T('cq.mode_changes'), ni(q.mode_changes.count)], [T('cq.wrong_edge'), mins(q.misarmed.minutes)],
        [T('cq.heat_losing'), mins(q.run_context.heating_minutes_house_losing_heat)], [T('cq.deferred'), T('cq.deferred_v', { n: q.would_have_deferred.heating_minutes })],
        [T('cq.guard'), ni(q.settling_guard.hold_episodes)]
      ];
      host.innerHTML = '<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:6px 16px;font-size:13px">' + items.map(function (i) {
        return '<div style="display:flex;justify-content:space-between;gap:8px;border-top:1px solid var(--rule);padding-top:5px"><span style="color:var(--ink2)">' + esc(i[0]) + '</span><span class="num">' + esc(i[1]) + '</span></div>';
      }).join('') + '</div>';
      // Issue #1475: these bars score the forecast v3 logs (pred_indoor); say so when v3 is not the main engine
      var prim = (D.acc && D.acc.primary_engine) || (D.engines && D.engines.v4 && D.engines.v4.prediction_engines && D.engines.v4.prediction_engines.primary);
      $('an-pe-src').textContent = prim && prim !== 'v3' ? T('cq.pe_src', { engine: iso(prim) }) : '';
      var H = q.prediction_error.horizons, rowsPe = [[T('cq.h30'), H['30m']], [T('cq.h60'), H['60m']], [T('cq.h2'), H['120m']], [T('cq.h4'), q.prediction_error.archive_lead]].filter(function (r) { return r[1]; });
      var mx = Math.max.apply(null, rowsPe.map(function (r) { return r[1].mean_abs_f; }).concat([1]));
      pe.innerHTML = rowsPe.map(function (r) {
        var v = r[1];
        return '<div style="display:grid;grid-template-columns:70px minmax(0,1fr) 110px;gap:8px;align-items:center;font-size:12px;margin-bottom:4px"><span style="color:var(--ink2)">' + esc(r[0]) +
          '</span><div style="height:10px;background:var(--chip);border-radius:3px"><div style="height:10px;border-radius:3px;background:var(--s2);width:' + Math.min(100, v.mean_abs_f / mx * 100) + '%"></div></div><span class="num">' +
          nf(ND.dFToDisp(v.mean_abs_f), 1) + '° (' + (v.mean_signed_f > 0 ? '+' : '') + nf(ND.dFToDisp(v.mean_signed_f), 1) + ')</span></div>';
      }).join('');
    }
    var ps = ND.status && ND.status.promise_summary, pr = $('an-promises');
    pr.innerHTML = ps && ps.promises && ps.promises.length ? ps.promises.map(function (p) {
      return '<div style="font-size:13px;display:flex;justify-content:space-between;gap:8px;border-top:1px solid var(--rule);padding-top:5px;margin-bottom:5px"><span>' +
        esc(String(p.label || p.promise).replace(/^(\d+): /, '#$1 ')) + '</span><span style="color:var(--ink2)">' + esc(p.last_hit ? T('promises.hits_last', { n: p.hits, when: ND.fmtDateTime(p.last_hit) }) : T('promises.hits', { n: p.hits })) + '</span></div>';
    }).join('') : '<div class="empty">' + esc(T('promises.none')) + '</div>';
  }

  // ---------- suggestions (respond_suggestion, exactly as classic) ----------
  function renderSuggestions() {
    var host = $('an-suggestions'), L = D.learning;
    if (!L) { err(host, D.errs[0] || T('no_data')); return; }
    var list = L.suggestions || [];
    if (!list.length) { host.innerHTML = '<div class="empty">' + esc(T('sug.none')) + '</div>'; return; }
    // Issue #1652: every suggestion is advice. "Got it" sends accept (an acknowledgement: no setting changes, the
    // suggestion is held back 30 days); Dismiss hides it.
    host.innerHTML = list.map(function (s) {
      var k = esc(s.key);
      return '<div class="kv" id="sug-' + k + '" style="grid-template-columns:1fr"><div>' + esc(s.text) + '</div><div class="row" id="sug-actions-' + k + '">' +
        '<button type="button" class="btn small primary" data-sug="' + k + '" data-act="accept">' + esc(T('sug.ack')) + '</button>' +
        '<button type="button" class="btn small" data-sug="' + k + '" data-act="dismiss">' + esc(T('sug.dismiss')) + '</button></div></div>';
    }).join('');
  }
  async function respond(key, action) {
    var box = $('sug-actions-' + key);
    try {
      await ND.api('respond_suggestion', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ action: action, suggestion_key: key }) });
      if (box) box.innerHTML = '<span class="badge ok">' + esc(T(action === 'accept' ? 'sug.acknowledged' : 'sug.dismissed')) + '</span> <span class="small">' + esc(T('sug.accurate')) + '</span> ' +
        '<button type="button" class="btn small" data-sug="' + esc(key) + '" data-fb="correct">' + esc(T('sug.correct')) + '</button> <button type="button" class="btn small" data-sug="' + esc(key) + '" data-fb="incorrect">' + esc(T('sug.incorrect')) + '</button>';
      setTimeout(function () { if (D.learning) { D.learning.suggestions = (D.learning.suggestions || []).filter(function (s) { return s.key !== key; }); renderSuggestions(); } }, 8000);
    } catch (e) { ND.toast(e.message, 'err'); }
  }
  async function feedback(key, verdict) {
    var box = $('sug-actions-' + key);
    try {
      await ND.api('respond_suggestion', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ feedback: verdict, suggestion_key: key }) });
      if (box) box.innerHTML = '<span class="badge ok">' + esc(T('sug.feedback')) + '</span>';
      setTimeout(function () { if (D.learning) { D.learning.suggestions = (D.learning.suggestions || []).filter(function (s) { return s.key !== key; }); renderSuggestions(); } }, 1500);
    } catch (e) { ND.toast(e.message, 'err'); }
  }

  // ---------- AI status line ----------
  function renderAiLine() {
    var a = D.ai && D.ai.status, el = $('an-ai-line');
    if (!a) { el.textContent = D.errs[4] ? T('ai.unavailable', { error: D.errs[4] }) : ''; return; }
    aiDisabled = a.status === 'disabled' || !a.status;
    // a.status / a.model / the breaker state are backend codes and names: shown as they are, isolated for RTL
    el.textContent = T('ai.line', { status: iso(a.status ? CAI18N.word('settings.ai.status.', a.status) : CAI18N.t('common.unknown')), has_model: a.model ? 'yes' : 'no', model: iso(a.model || ''),
      has_limit: a.investigator_requests_limit != null ? 'yes' : 'no', today: a.investigator_requests_today || 0, limit: a.investigator_requests_limit == null ? 0 : a.investigator_requests_limit,
      cost: CAI18N.fmt.number(a.monthly_cost_estimate || 0, { style: 'currency', currency: 'USD' }), errors: a.error_count || 0,
      has_circuit: a.circuit_breaker_state && a.circuit_breaker_state !== 'closed' ? 'yes' : 'no', circuit: iso(CAI18N.word('settings.ai.breaker.', a.circuit_breaker_state || '')) });
    applyAiState();
  }
  function applyAiState() {
    var isAi = $('report-type').value !== 'activity_record';
    $('ai-disabled-notice').hidden = !(isAi && aiDisabled);
    $('run-report-btn').disabled = isAi && aiDisabled;
  }

  // ---------- reports ----------
  function updateReportType() {
    var type = $('report-type').value, tw = $('report-time-window');
    if (type === 'activity_record') {
      tw.innerHTML = [[6, 'h6'], [12, 'h12'], [24, 'h24'], [48, 'h48'], [168, 'd7']].map(function (o) {
        return '<option value="' + o[0] + '"' + (o[0] === 12 ? ' selected' : '') + '>' + esc(T('rep.window.' + o[1])) + '</option>';
      }).join('');
      $('investigation-focus-controls').hidden = true; $('run-report-btn').textContent = T('rep.generate');
    } else {
      tw.innerHTML = [[24, 'd1'], [168, 'd7'], [720, 'd30']].map(function (o) {
        return '<option value="' + o[0] + '"' + (o[0] === 24 ? ' selected' : '') + '>' + esc(T('rep.window.' + o[1])) + '</option>';
      }).join('');
      $('investigation-focus-controls').hidden = false; $('run-report-btn').textContent = T('rep.investigate');
    }
    applyAiState();
  }
  function reset(label) { var b = $('run-report-btn'); b.disabled = false; b.textContent = T(label === 'Generate' ? 'rep.generate' : 'rep.investigate'); $('report-loading').textContent = T('rep.generating'); $('report-loading').hidden = true; applyAiState(); }
  // The Activity record as a table in the viewer's language: each row's event / settings is its `_msg` wire message
  // (CAI18N.text), the notices are `limited_msg` / `whf_warning_msg`; the English markdown `table` stays for copy, download
  // and the GitHub issue. The diagnostic footer (English, appended after the last table row) is shown below.
  function activityHtml(resp) {
    function bold(s) { return esc(s).replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>'); }
    var rows = resp.rows || [], h = '';
    if (resp.whf_warning_msg) h += '<div class="banner warn">' + esc(CAI18N.tw(resp.whf_warning_msg)) + '</div>';
    if (resp.limited_msg) h += '<div class="note">' + esc(CAI18N.tw(resp.limited_msg)) + '</div>';
    if (!rows.length) return h + '<div class="empty">' + esc(resp.no_events_msg ? CAI18N.tw(resp.no_events_msg) : T('rep.no_events')) + '</div>';
    h += '<table><thead><tr>' + ['time', 'event', 'settings', 'source', 'indoor', 'outdoor'].map(function (c) { return '<th scope="col">' + esc(T('rep.act_col_' + c)) + '</th>'; }).join('') + '</tr></thead><tbody>' +
      rows.map(function (r) {
        // Issue #1674: a 7-day record spans days, so each row shows its date (`ts`, the viewer's language); `time`
        // (the English "Sat 11-21 10:31" stamp) is the fallback for a row whose timestamp could not be read.
        return '<tr><td><bdi>' + esc(r.ts ? ND.fmtDateTime(r.ts) : r.time) + '</bdi></td><td>' + bold(CAI18N.text(r, 'event')) + '</td><td>' + bold(CAI18N.text(r, 'settings')) + '</td><td>' + esc(CAI18N.word('dashboard.analysis.source.', r.source)) + '</td><td><bdi>' + esc(r.indoor) + '</bdi></td><td><bdi>' + esc(r.outdoor) + '</bdi></td></tr>';
      }).join('') + '</tbody></table>';
    var lines = String(resp.table || '').split('\n'), last = -1;
    lines.forEach(function (l, i) { if (l.trim().charAt(0) === '|') last = i; });
    var footer = last >= 0 ? lines.slice(last + 1).join('\n').trim() : '';
    return h + (footer ? '<div class="sub" style="margin-top:8px">' + ND.md(footer) + '</div>' : '');
  }
  async function runActivity() {
    var hours = parseInt($('report-time-window').value, 10) || 12, btn = $('run-report-btn');
    btn.disabled = true; btn.textContent = T('rep.generating'); $('report-loading').hidden = false; $('report-status').textContent = '';
    try {
      var resp = await ND.api('activity_record?hours=' + hours);
      activityTable = resp.table || '';
      $('report-content').innerHTML = resp.rows ? '<div>' + activityHtml(resp) + '</div>'
        : activityTable ? '<div>' + ND.md(activityTable) + '</div>' : '<div class="empty">' + esc(T('rep.no_events')) + '</div>'; // an older backend: the markdown table
      $('report-actions').hidden = false;
    } catch (e) { $('report-status').textContent = T('rep.error', { error: e.message }); } finally { reset('Generate'); }
  }
  function parseSections(text) {
    var sections = {}, order = [], key = null, lines = [];
    function flush() { if (key !== null) sections[key] = lines.join('\n').trim(); }
    text.split('\n').forEach(function (line) {
      var s = line.trim();
      if (s.indexOf('## ') === 0) { flush(); lines = []; key = HEADER_MAP[s.slice(3).trim().toUpperCase()] || null; if (key && order.indexOf(key) < 0) order.push(key); }
      else if (key !== null) lines.push(line);
    });
    flush();
    return { sections: sections, order: order, active: key };
  }
  function renderProgress(content, acc, prefix) {
    var p = parseSections(acc);
    prefix = prefix || '';
    if (!p.order.length) { content.innerHTML = prefix + '<div class="empty">' + esc(T('rep.writing')) + '</div>'; return; }
    content.innerHTML = prefix + p.order.map(function (k) {
      var def = SECTIONS.filter(function (d) { return d.key === k; })[0]; if (!def) return '';
      return '<details open><summary><strong>' + esc(T('section.' + def.key)) + '</strong>' + (k === p.active ? ' <span class="sub">' + esc(T('rep.writing_short')) + '</span>' : '') + '</summary><div class="note">' + ND.md(p.sections[k] || '') + '</div></details>';
    }).join('');
  }
  // ---------- AI provider limits and notices (Issue #1336) ----------
  // Before a run, GET ai_limits (zone-scoped like ai_status). A readable limit that is too small shows a warning with
  // Run anyway / Cancel; an unreadable one shows a one-line note and the run goes ahead; a failed request goes ahead
  // silently. The check only ever warns. Error codes and the cut-short flag from the stream are shown as notices.
  function A(key, params) { return CAI18N.t('ai.' + key, params); }
  var AI_REQ = { output: 8192, context: 16384, combined: 24576 }; // const.AI_REQUIRED_* (docs/ai-integration.md)
  var lastLimits = null;
  // Issue #1660: built-in Claude's limit is Climate Advisor's own "Maximum Response Length" (AI settings), named as the
  // options flow shows it, never as the raw option key and domain ("max_tokens in climate_advisor").
  function builtinLimits(l) { return !!l && l.provider === 'builtin_claude'; }
  function limitsIntegration(l) { return builtinLimits(l) ? A('limits.builtin_place') : iso((l && l.integration) || '\u2014'); }
  function limitsSetting(l, c) { return builtinLimits(l) ? A('limits.builtin_setting') : iso(String(c.setting || '')); }
  function failedChecks(l) { return (l && Array.isArray(l.checks) ? l.checks : []).filter(function (c) { return c && c.ok === false; }); }
  function checkLine(l, c) {
    return A('limits.check', { kind: c.kind === 'output' || c.kind === 'context' ? c.kind : 'combined', setting: limitsSetting(l, c),
      integration: limitsIntegration(l), value: Number(c.value) || 0, required: Number(c.required) || 0 });
  }
  // status lines: the catalog message for `key` (with `params`), else the backend's `text`
  function aiStatusText(ev) {
    return ev && ev.key && CAI18N.has(ev.key) ? CAI18N.tw({ key: ev.key, params: ev.params || {} }) : String((ev && ev.text) || '');
  }
  // the occupant notice for an AI_ERROR_* / AI_CUT_SHORT code, or null for no / an unknown code
  function aiNotice(code) {
    if (!code) return null;
    if (code === 'ai_output_limit') return A('notice.ai_output_limit', { required: AI_REQ.output });
    if (code === 'ai_context_limit') return A('notice.ai_context_limit', { context: AI_REQ.context, combined: AI_REQ.combined });
    if (code === 'ai_report_cut_short') {
      var c = failedChecks(lastLimits)[0];
      return c ? A('notice.ai_report_cut_short', { has_setting: 'yes', setting: limitsSetting(lastLimits, c), integration: limitsIntegration(lastLimits), required: Number(c.required) || AI_REQ.output })
        : A('notice.ai_report_cut_short', { has_setting: 'no', setting: '', integration: '', required: AI_REQ.output });
    }
    return CAI18N.has('ai.notice.' + code) ? A('notice.' + code) : null;
  }
  function cutShortCode(res) { return res && res.cut_short ? (res.cut_short_code || 'ai_report_cut_short') : null; }
  // Resolves 'run' or 'cancel'. A note (unreadable limits) is returned for the caller to show while the run goes ahead.
  async function preRunLimits(content) {
    var l;
    lastLimits = null;
    try { l = await ND.api('ai_limits'); } catch (e) { return { go: 'run', note: '' }; } // the check never blocks
    if (!l || typeof l !== 'object') return { go: 'run', note: '' };
    lastLimits = l;
    if (l.readable === false) return { go: 'run', note: A('limits.unreadable') };
    var failed = l.ok === false ? failedChecks(l) : [];
    if (!failed.length) return { go: 'run', note: '' };
    return new Promise(function (resolve) {
      var el = document.createElement('div');
      el.className = 'banner warn'; el.setAttribute('role', 'alert');
      el.innerHTML = '<div class="b-text"><strong>' + esc(A('limits.title')) + '</strong><ul>' +
        failed.map(function (c) { return '<li>' + esc(checkLine(l, c)) + '</li>'; }).join('') + '</ul><div>' + esc(A('limits.advice')) + '</div></div>';
      [['cancel', 'btn small', 'limits.cancel'], ['run', 'btn small primary', 'limits.run_anyway']].forEach(function (b) {
        var c = document.createElement('button'); c.type = 'button'; c.className = b[1]; c.textContent = A(b[2]);
        c.setAttribute('data-ai-limits', b[0]);
        c.addEventListener('click', function () { el.remove(); resolve({ go: b[0], note: '' }); });
        el.appendChild(c);
      });
      content.innerHTML = ''; content.appendChild(el);
      var run = el.querySelector('[data-ai-limits="run"]'); if (run && run.focus) run.focus();
    });
  }
  async function runInvestigation() {
    var btn = $('run-report-btn'), loading = $('report-loading'), status = $('report-status'), content = $('report-content');
    var focus = $('investigation-focus').value.trim(), hours = parseInt($('report-time-window').value, 10) || 24, deep = $('investigation-deep').checked;
    btn.disabled = true; btn.textContent = T('rep.running'); loading.hidden = false; loading.textContent = A('limits.checking'); status.textContent = ''; content.innerHTML = ''; $('report-actions').hidden = true;
    var pre = await preRunLimits(content);
    if (pre.go !== 'run') { status.textContent = A('limits.cancelled'); reset('Investigate'); return; }
    loading.hidden = false; loading.textContent = T('rep.connecting');
    var acc = '', noteHtml = pre.note ? '<div class="note">' + esc(pre.note) + '</div>' : '';
    if (noteHtml) content.innerHTML = noteHtml;
    try {
      var body = { hours: hours }; if (focus) body.focus = focus; if (deep) body.deep = true;
      var resp = await ND.stream('ai_investigate', body), reader = resp.body.getReader(), dec = new TextDecoder(), buf = '';
      for (;;) {
        var r = await reader.read(); if (r.done) break;
        buf += dec.decode(r.value, { stream: true });
        var evs = buf.split('\n\n'); buf = evs.pop();
        for (var i = 0; i < evs.length; i++) {
          if (evs[i].indexOf('data: ') !== 0) continue;
          var ev; try { ev = JSON.parse(evs[i].slice(6)); } catch (e) { continue; }
          if (ev.type === 'thinking') loading.textContent = T('rep.thinking', { text: ev.text.replace(/[\n]+/g, ' ').trim().slice(-180) });
          else if (ev.type === 'status') loading.textContent = aiStatusText(ev);
          else if (ev.type === 'chunk') { var first = !acc.length; acc += ev.text; renderProgress(content, acc, noteHtml); if (first) loading.hidden = true; status.textContent = T('rep.receiving', { n: acc.length }); }
          else if (ev.type === 'done') {
            var result = { success: ev.success !== false, source: ev.source || 'ai', data: ev.data || {}, error: ev.error || null, raw_response: ev.raw_response || acc, input_context: ev.input_context || '', truncated: !!ev.truncated, truncated_empty: !!ev.truncated_empty, cut_short: !!ev.cut_short, cut_short_code: ev.cut_short_code || null, error_code: ev.error_code || null };
            currentReport = { reportType: 'investigation', timestamp: new Date().toISOString(), result: result, focus: focus };
            renderReport(currentReport);
            status.textContent = result.truncated_empty ? T('rep.truncated_empty_status')
              : result.truncated ? T('rep.truncated_status')
              : T('rep.completed', { time: CAI18N.fmt.time(new Date()), source: iso(CAI18N.word('dashboard.analysis.source.', result.source)) });
            loadHistory();
          } else if (ev.type === 'error') throw new Error(ev.message || T('rep.failed'), { cause: ev });
        }
      }
      if (!currentReport && acc) { content.innerHTML = noteHtml + '<pre style="white-space:pre-wrap;font-size:12px">' + esc(acc) + '</pre>'; status.textContent = T('rep.stream_ended'); }
    } catch (e) {
      // "AI / investigator not enabled" is the endpoint's HTTP 403 (errors.ai_not_enabled / errors.investigator_not_enabled in
      // `message_msg`); a failure inside the stream is an SSE error event (plain text, no code). The 403 body is shown in the
      // viewer's language through its `message_msg`.
      // Issue #1336: an SSE error event (or a JSON error body) with an `error_code` also gets the occupant notice.
      var notEnabled = !!e && e.status === 403, code = (e && e.cause && e.cause.error_code) || (e && e.body && e.body.error_code) || null;
      var notice = aiNotice(code);
      status.textContent = T('rep.error_not_enabled', { error: (e && e.body && ND.text(e.body, 'message')) || e.message, hint: notEnabled ? 'yes' : 'no' });
      content.innerHTML = notice ? '<div class="banner warn" role="alert"><div class="b-text">' + esc(notice) + '</div></div>' : '';
    } finally { reset('Investigate'); }
  }
  function renderReport(entry) {
    var content = $('report-content'), res = entry.result || {}, ts = entry.timestamp ? CAI18N.fmt.dateTime(new Date(entry.timestamp)) : '';
    if (!res.success) {
      var fn = aiNotice(res.error_code);
      content.innerHTML = (fn ? '<div class="banner warn" role="alert"><div class="b-text">' + esc(fn) + '</div></div>' : '') + '<div class="err">' + esc(T('rep.failed_report', { source: iso(CAI18N.word('dashboard.analysis.source.', res.source || 'error')), error: res.error || T('rep.unknown_error') })) + '</div>';
      $('report-actions').hidden = true; return;
    }
    var data = res.data || {}, h = (ts ? '<div class="sub">' + esc(ts) + (res.is_refined ? ' <span class="badge">' + esc(T('rep.refined')) + '</span>' : '') + '</div>' : '');
    if (res.truncated_empty) h += '<div class="banner warn">' + esc(T('rep.truncated_empty')) + '</div>';
    else if (res.truncated) h += '<div class="banner warn">' + esc(T('rep.truncated')) + '</div>';
    // Issue #1336: a reply missing required sections (or stopped at a provider limit) names the setting to raise
    var csn = aiNotice(cutShortCode(res)), ecn = res.error_code && res.error_code !== cutShortCode(res) ? aiNotice(res.error_code) : null;
    if (csn) h += '<div class="banner warn">' + esc(csn) + '</div>';
    if (ecn) h += '<div class="banner warn">' + esc(ecn) + '</div>';
    h += '<div class="small">' + esc(T(res.source === 'fallback' ? 'rep.fallback' : 'rep.ai_generated')) + '</div>';
    var any = false;
    SECTIONS.forEach(function (d) { var tx = data[d.key] || ''; if (!tx) return; any = true; h += '<details open><summary><strong>' + esc(T('section.' + d.key)) + '</strong></summary><div class="note">' + ND.md(tx) + '</div></details>'; });
    if (!any && res.raw_response) h += '<div class="note">' + ND.md(res.raw_response) + '</div>';
    content.innerHTML = h; $('report-actions').hidden = false;
  }
  function formatReport() {
    if (!currentReport) return '';
    // The downloaded Markdown and the GitHub issue body are records, not screens: they stay English (Issue #1341 non-goals).
    var ts = currentReport.timestamp ? new Date(currentReport.timestamp).toLocaleString('en') : 'Unknown time', res = currentReport.result || {}, data = res.data || {};
    var out = '# Climate Advisor — Investigation Report\n\n| Field | Value |\n|---|---|\n| **Date** | ' + ts + ' |\n| **Source** | ' + (res.source || 'unknown') + ' |\n';
    if (currentReport.focus) out += '| **Focus** | ' + currentReport.focus + ' |\n';
    if (res.truncated_empty) out += '| **Truncated** | Yes — zero visible output despite using the full response budget |\n'; else if (res.truncated) out += '| **Truncated** | Yes — hit max response length |\n';
    out += '\n---\n\n';
    SECTIONS.forEach(function (d) { var c = (data[d.key] || '').trim(); if (c) out += '## ' + d.label + '\n\n' + c + '\n\n'; });
    if (res.raw_response && !data.incongruities) out += '## Raw Response\n\n```\n' + res.raw_response + '\n```\n\n';
    return out + '---\n\n*Generated by Climate Advisor — Home Assistant Integration*\n';
  }
  function repText() { return $('report-type').value === 'activity_record' ? activityTable : formatReport(); }
  async function downloadFullLogs() {
    var r = await Promise.allSettled([ND.api('event_log?hours=168'), ND.api('investigation_reports')]);
    var bundle = r[0].status === 'fulfilled' ? r[0].value : {};
    if (r[1].status === 'fulfilled') bundle.investigation_reports = r[1].value;
    ND.download(JSON.stringify(bundle, null, 2), 'climate-advisor-logs-' + new Date().toISOString().slice(0, 10) + '.json', 'application/json');
  }
  ND.downloadFullLogs = downloadFullLogs;

  // ---------- GitHub issue modal (submit_github_issue, as classic) ----------
  function openIssue(prefill) {
    var m = $('gh-modal');
    if (prefill) { $('github-issue-title').value = prefill.title; $('github-issue-body').value = prefill.body; $('github-issue-labels').value = 'bug'; }
    $('github-issue-status').textContent = ''; $('github-modal-not-configured').hidden = true; $('github-submit-btn').disabled = false;
    if (m.showModal) m.showModal(); else m.setAttribute('open', '');
    $('github-issue-title').focus();
  }
  function closeIssue() { var m = $('gh-modal'); if (m.close) m.close(); else m.removeAttribute('open'); }
  function issueFromReport(entry) {
    var saved = currentReport; currentReport = entry || currentReport;
    if (!currentReport) { currentReport = saved; return; }
    var ts = currentReport.timestamp ? new Date(currentReport.timestamp).toLocaleDateString() : new Date().toLocaleDateString();
    openIssue({ title: 'AI Investigative Analysis - ' + ts, body: formatReport() });
  }
  async function appendIncident(body) {
    try {
      var ev = await ND.api('event_log?hours=2'), inc = (ev.events || []).filter(function (e) { return e.type === 'incident_detected'; });
      if (inc.length) body += '\n\n## Incident Package\n<details>\n<summary>Diagnostic data for scenario reproduction</summary>\n\n```json\n' + JSON.stringify(inc[inc.length - 1], null, 2) + '\n```\n</details>';
    } catch (e) { /* non-fatal */ }
    return body;
  }
  async function submitIssue() {
    var st = $('github-issue-status'), btn = $('github-submit-btn'), title = $('github-issue-title').value.trim();
    var body = $('github-issue-body').value.trim(), labels = $('github-issue-labels').value.split(',').map(function (l) { return l.trim(); }).filter(Boolean);
    if (!title) { st.textContent = T('issue.title_required'); return; }
    st.textContent = T('issue.submitting'); btn.disabled = true;
    body = await appendIncident(body);
    var token = await ND.getToken();
    try {
      var resp = await fetch('/api/climate_advisor/' + await ND.scopedPath('submit_github_issue'), { method: 'POST', headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + token }, body: JSON.stringify({ title: title, body: body, labels: labels }) });
      var data = await resp.json();
      if (data.success) { st.innerHTML = esc(T('issue.created', { link: '\u0001' })).replace('\u0001', '<a href="' + esc(data.issue_url) + '" target="_blank" rel="noopener"><bdi>#' + esc(data.issue_number) + '</bdi></a>'); $('gh-cancel').textContent = T('issue.close'); }
      else if (data.error === 'not_configured') { $('github-modal-not-configured').hidden = false; st.textContent = ''; btn.disabled = false; }
      else { st.textContent = T('issue.failed', { error: data.error || T('issue.unknown_error') }); btn.disabled = false; }
    } catch (e) { st.textContent = T('issue.network_error', { error: e.message }); btn.disabled = false; }
  }

  // ---------- history ----------
  async function loadHistory() {
    var host = $('unified-history-list');
    try {
      var d = D.reports || await ND.api('investigation_reports');
      allHistory = (Array.isArray(d) ? d : []).map(function (e) { return Object.assign({}, e, { report_type: e.report_type || 'investigation' }); })
        .sort(function (a, b) { return new Date(b.timestamp) - new Date(a.timestamp); });
      D.reports = null; // later calls refetch
      renderHistory();
    } catch (e) { host.innerHTML = '<div class="empty">' + esc(T('hist.load_failed')) + '</div>'; }
  }
  function renderHistory() {
    var host = $('unified-history-list'), f = $('hist-from-date').value, t = $('hist-to-date').value;
    var from = f ? new Date(f) : null, to = t ? new Date(t + 'T23:59:59') : null;
    filtered = allHistory.filter(function (e) {
      if (histFilter !== 'all' && (e.report_type || 'activity') !== histFilter) return false;
      var ts = new Date(e.timestamp); return !(from && ts < from) && !(to && ts > to);
    });
    if (!filtered.length) { host.innerHTML = '<div class="empty">' + esc(T(allHistory.length ? 'hist.no_match' : 'hist.none')) + '</div>'; return; }
    var shown = Math.min(histShowing, filtered.length);
    host.innerHTML = filtered.slice(0, shown).map(function (e, i) {
      var res = e.result || {}, sum = res.data && res.data.activity_summary ? res.data.activity_summary.replace(/\s+/g, ' ').trim() : '';
      if (sum.length > 110) sum = sum.slice(0, 110) + '...';
      return '<div class="hist-row"><div class="body"><div class="row" style="gap:6px"><strong style="font-size:13px">' + esc(e.timestamp ? CAI18N.fmt.dateTime(new Date(e.timestamp)) : T('hist.unknown_time')) + '</strong>' +
        '<span class="badge">' + esc(T(e.report_type === 'investigation' ? 'hist.investigation' : 'hist.activity')) + '</span>' + (res.truncated_empty ? '<span class="badge changed">' + esc(T('hist.no_output')) + '</span>' : res.truncated ? '<span class="badge changed">' + esc(T('hist.truncated')) + '</span>' : '') +
        '<span class="sub">' + esc(CAI18N.word('dashboard.analysis.source.', res.source || 'unknown')) + '</span></div>' + (sum ? '<div class="small">' + esc(sum) + '</div>' : '') + '</div>' +
        '<div class="acts"><button type="button" class="btn small" data-h="open" data-i="' + i + '">' + esc(T('hist.open')) + '</button><button type="button" class="btn small" data-h="copy" data-i="' + i + '">' + esc(T('hist.copy')) + '</button>' +
        '<button type="button" class="btn small" data-h="issue" data-i="' + i + '">' + esc(T('hist.issue')) + '</button><button type="button" class="btn small danger" data-h="del" data-i="' + i + '" aria-label="' + esc(T('hist.delete_aria')) + '">' + esc(T('hist.delete')) + '</button></div></div>';
    }).join('') + (filtered.length > shown ? '<button type="button" class="btn small" id="hist-more" style="margin-top:8px">' + esc(T('hist.more', { n: filtered.length - shown })) + '</button>' : '');
  }
  function asCurrent(e) { return { reportType: e.report_type || 'activity', timestamp: e.timestamp, result: e.result, focus: e.focus }; }
  async function histClick(btn) {
    var e = filtered[+btn.getAttribute('data-i')]; if (!e) return;
    var what = btn.getAttribute('data-h');
    if (what === 'open') { currentReport = asCurrent(e); renderReport(currentReport); $('report-content').scrollIntoView({ behavior: 'smooth' }); }
    else if (what === 'copy') { var s = currentReport; currentReport = asCurrent(e); var tx = formatReport(); currentReport = s; ND.copy(tx); }
    else if (what === 'issue') issueFromReport(asCurrent(e));
    else if (what === 'del') {
      try {
        await ND.api('delete_report', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ report_type: e.report_type || 'activity', timestamp: e.timestamp }) });
        if (currentReport && currentReport.timestamp === e.timestamp) { currentReport = null; $('report-content').innerHTML = '<div class="empty">' + esc(T('hist.deleted')) + '</div>'; $('report-actions').hidden = true; }
        D.reports = null; await loadHistory();
      } catch (er) { ND.toast(T('hist.delete_failed', { error: er.message }), 'err'); }
    }
  }

  // ---------- prediction accuracy (Issue #1497: the full view, drawn here, never in a frame) ----------
  // The dashboard already sits in Home Assistant's panel frame; a page framed inside it could not reach HA's login
  // (Issue #1491: no token in the iOS app -> 401). The view is the shared pa_view.js, the same code the stand-alone
  // page mounts, drawn straight into this card on first show and kept on the dashboard's zone. The link still opens
  // the stand-alone page in this same frame.
  var paMounted = false;
  function loadAccuracy() {
    $('prediction-accuracy-link').href = 'prediction_accuracy.html' + (ND.zone ? '?zone=' + encodeURIComponent(ND.zone) : '');
    var PA = window.CAPredictionAccuracy;
    if (!PA) { $('an-pa-view').innerHTML = '<div class="err">' + esc(T('pa.load_failed')) + '</div>'; return; }
    if (!paMounted) { paMounted = true; PA.mount($('an-pa-view'), { zone: ND.zone, embedded: true }); }
    else if (ND.zone) PA.setZone(ND.zone);
  }

  // ---------- wiring ----------
  function init() {
    $('prediction-accuracy-link').addEventListener('click', function () { loadAccuracy(); ND.rememberReturnTab('analysis'); });
    $('report-type').addEventListener('change', updateReportType);
    $('run-report-btn').addEventListener('click', function () { return $('report-type').value === 'activity_record' ? runActivity() : runInvestigation(); });
    $('rep-copy').addEventListener('click', function () { var t = repText(); if (t) ND.copy(t); });
    $('rep-download').addEventListener('click', function () {
      var act = $('report-type').value === 'activity_record', t = repText(); if (!t) return;
      var date = (act ? new Date() : new Date(currentReport.timestamp)).toISOString().slice(0, 10);
      ND.download(t, 'climate-advisor-' + (act ? 'activity-record' : 'investigation') + '-' + date + '.md', 'text/markdown');
    });
    $('rep-fulllogs').addEventListener('click', downloadFullLogs);
    $('rep-issue').addEventListener('click', function () {
      if ($('report-type').value === 'activity_record') {
        if (!activityTable) return;
        openIssue({ title: 'Activity Record — ' + new Date().toLocaleDateString(), body: '## Activity Record\n\n' + activityTable + '\n\n---\n*Generated by Climate Advisor*' });
      } else issueFromReport();
    });
    $('github-submit-btn').addEventListener('click', submitIssue);
    $('gh-cancel').addEventListener('click', closeIssue);
    $('gh-modal').addEventListener('click', function (e) { if (e.target === this) closeIssue(); });
    function setFilter(t) {
      histFilter = t; histShowing = 5;
      ['all', 'investigation'].forEach(function (k) { $('hist-filter-' + k).setAttribute('aria-pressed', k === t ? 'true' : 'false'); });
      renderHistory();
    }
    $('hist-filter-all').addEventListener('click', function () { setFilter('all'); });
    $('hist-filter-investigation').addEventListener('click', function () { setFilter('investigation'); });
    ['hist-from-date', 'hist-to-date'].forEach(function (id) { $(id).addEventListener('input', renderHistory); $(id).addEventListener('change', renderHistory); });
    $('hist-clear').addEventListener('click', function () { $('hist-from-date').value = ''; $('hist-to-date').value = ''; renderHistory(); });
    $('unified-history-list').addEventListener('click', function (e) {
      var b = e.target.closest('button'); if (!b) return;
      if (b.id === 'hist-more') { histShowing = 20; renderHistory(); } else if (b.getAttribute('data-h')) histClick(b);
    });
    $('an-suggestions').addEventListener('click', function (e) {
      var b = e.target.closest('button[data-sug]'); if (!b) return;
      if (b.getAttribute('data-fb')) feedback(b.getAttribute('data-sug'), b.getAttribute('data-fb')); else respond(b.getAttribute('data-sug'), b.getAttribute('data-act'));
    });
    updateReportType();
  }

  ND.register('analysis', {
    show: function () { if (!loaded || ND._anZone !== ND.zone) { ND._anZone = ND.zone; load(); } loadAccuracy(); },
    refresh: load
  });
  ND.on('init', init);
  ND.on('zone', function () { if (ND.active === 'analysis') { ND._anZone = ND.zone; load(); loadAccuracy(); } else loaded = false; });
  ND.on('refresh', function () { if (ND.active === 'analysis') load(); });
  ND.on('state-changed', function () { if (ND.active === 'analysis') load(); });
  ND.on('status', function () { if (loaded) { renderKpis(); renderControl(); } });
})();
