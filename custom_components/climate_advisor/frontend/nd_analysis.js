/* Issue #1230 R5: Analysis tab - is it working, and what can improve?
   Reads only existing endpoints: learning, engines, event_log (diagnostics.control_quality), chart_data
   (thermal_model), ai_status, investigation_reports. Writes only what classic writes: respond_suggestion,
   activity_record / ai_investigate (report generation), delete_report, submit_github_issue. */
(function () {
  'use strict';
  var $ = ND.$, esc = ND.esc;
  var D = {};              // last loaded payloads
  var currentReport = null, activityTable = null;
  var allHistory = [], filtered = [], histFilter = 'all', histShowing = 5, aiDisabled = false;
  var loaded = false, loadSeq = 0;

  var REASONS = {
    indoor_unavailable: 'Indoor sensor was unavailable', insufficient_samples: 'Window ended before enough samples collected',
    window_elapsed_too_few_samples: 'Window closed before enough samples collected', insufficient_decay: "Temperature didn't move enough to measure",
    ols_bad_fit: 'Reading was too noisy to trust', poor_fit: 'Reading was too noisy to trust', state_changed: 'HVAC, fan, or window state changed mid-window',
    outlier: 'Result was an outlier vs prior measurements', abandoned: 'Measurement interrupted',
    cycle_too_short: 'Heating or cooling cycle was too short to measure', cycle_signal_below_resolution: 'Room moved fewer than three sensor steps, too little to show a rate',
    cycle_excluded_window_fan: 'A window was open or the fan ran during the cycle', cycle_setpoint_change: 'The setpoint changed and the run before the change was too short to measure',
    cycle_gap_restart: 'A restart cut the cycle short', cycle_thermostat_unavailable: 'The thermostat was unavailable during the cycle',
    cycle_no_off_edge: 'The cycle had no end to measure to', cycle_sensor_swap: 'Recorded on an earlier indoor sensor than the one in use now',
    hvac_run_too_short: 'Heating or cooling run was too short to measure (old method)', hvac_signal_below_resolution: 'Room temperature moved less than the sensor can reliably show (old method)',
    no_k_passive: 'House leak rate not learned yet, so this run\'s rate could not be separated', new_session_started: 'A new run started before this one could be measured',
    fan_activated: 'The fan started during the run', hvac_mode_switch: 'Heat and cool switched during the run', ols_bounds: 'Result was outside the believable range'
  };
  var OBS = {
    passive_decay: 'Passive windows', vent_window_decay: 'Window ventilation', vent_fan_decay: 'Fan/WHF ventilation', solar_gain: 'Solar readings',
    hvac_heat: 'Heating cycles', hvac_cool: 'Cooling cycles', fan_only_decay: 'Fan-only windows (legacy)', ventilated_decay: 'Ventilated windows (legacy)'
  };
  var SECTIONS = [
    { key: 'activity_summary', label: 'Activity summary' }, { key: 'incongruities', label: 'Incongruities found' },
    { key: 'data_quality', label: 'Data quality issues' }, { key: 'errors_warnings', label: 'System errors / warnings' },
    { key: 'hypotheses', label: 'Hypotheses' }, { key: 'recommended_actions', label: 'Recommended actions' },
    { key: 'assumptions', label: 'Assumptions & confidence' }
  ];
  var HEADER_MAP = {
    'ACTIVITY SUMMARY': 'activity_summary', 'INCONGRUITIES FOUND': 'incongruities', 'DATA QUALITY ISSUES': 'data_quality',
    'SYSTEM ERRORS / WARNINGS': 'errors_warnings', 'HYPOTHESES': 'hypotheses', 'RECOMMENDED ACTIONS': 'recommended_actions',
    'ASSUMPTIONS & CONFIDENCE': 'assumptions'
  };

  function pct(v) { return v == null ? '—' : Math.round(v * 100) + '%'; }
  function hm(s) { return s ? String(s).slice(0, 5).replace(/^0/, '') : '—'; }
  function yn(v) { return v ? 'yes' : 'no'; }
  function span(a, b) { return a ? ND.fmtTime(a) + '–' + ND.fmtTime(b) : '—'; }
  function r0(v) { return v == null ? '—' : String(Math.round(v)); }
  function tdisp(f, d) { return f == null ? '—' : ND.num(ND.fToDisp(f), d == null ? 0 : d); }
  function err(el, msg) { el.innerHTML = '<div class="err">Failed to load: ' + esc(msg) + '</div>'; }

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
    if (!L) { host.innerHTML = '<div class="tile"><div class="l">Headline numbers</div><div class="err">' + esc(D.errs[0] || 'No data') + '</div></div>'; return; }
    var c = L.compliance || {}, active = c.status === 'active';
    var ps = ND.status && ND.status.promise_summary;
    var today = L.today_record ? L.today_record.manual_overrides || 0 : 0;
    // Issue #1475: every score is over the most recent window_days records (14), not every recorded day
    var wd = c.window_days || Math.min(14, c.days_recorded || 0), last = 'last ' + wd + ' days';
    var pe = primaryMiss(), lastHit = promiseLastHit(ps);
    var tiles = [
      { l: 'Comfort score', v: active ? pct(c.comfort_score) : '—', n: last + ' (' + (c.days_recorded || 0) + ' recorded)',
        t: '% of time the indoor temperature stayed within the target range in effect, over the last ' + wd + ' days' },
      { l: 'Window advice followed', v: active ? pct(c.window_compliance) : '—', n: c.window_compliance_denominator != null ? c.window_compliance_denominator + ' days with advice, ' + last : '' },
      { l: 'Runtime per day', v: active ? Math.round(c.avg_daily_hvac_runtime_minutes || 0) + ' min' : '—', n: 'average HVAC, ' + last +
        (active && c.avg_daily_thermostat_fan_only_runtime_minutes ? ', fan-only ' + Math.round(c.avg_daily_thermostat_fan_only_runtime_minutes) + ' min' : '') },
      { l: 'Manual changes', v: active ? String(c.total_manual_overrides || 0) : '—', n: last + ' · ' + today + ' today' },
      // Issue #1490: misses over the last 30 days only (older misses age out), not a lifetime total
      { l: 'Promises missed', v: ps ? String(ps.total_hits || 0) : '0', n: ps && ps.total_hits ? 'last 30 days' + (lastHit ? ' · last ' + ND.fmtDateTime(lastHit) : '') : 'none in the last 30 days',
        t: 'Times a household promise was found broken over the last 30 days; each miss drops off 30 days after the day it happened' },
      { l: 'Prediction error, 4 h', v: pe ? ND.num(pe.mae, 1) + '°' : '—',
        n: pe ? pe.engine + ', last 24 h · runs ' + (pe.bias > 0 ? '+' : '') + ND.num(pe.bias, 1) + '° ' + (pe.bias > 0 ? 'warm' : 'cool') : 'not enough data yet',
        t: 'How far the main prediction 4 hours ahead was from what happened, over the last 24 hours (Debug > Backtest for more)' }
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
    if (!L) { err(host, D.errs[0] || 'no data'); return; }
    var y = L.yesterday_record, t = L.today_record, n = L.tomorrow_plan, u = ND.sym(L.unit || ND.unit), TL = L.today_live || {};
    var lo = L.comfort_range_low, hi = L.comfort_range_high;
    var tip = 'Time the indoor temperature was outside the target range in effect at the time (your comfort range' +
      (lo != null && hi != null ? ' ' + lo + '–' + hi + u : '') + ' while awake; your sleep/away range during planned setbacks)';
    function pair(rec, a, b) { return tdisp(rec[a]) + '→' + tdisp(rec[b]); }
    function hl(rec) {
      // Issue #1475: today's observed extremes fill only at end of day; show the outdoor high/low seen so far
      if (rec === t && TL.observed_high_f != null && rec.observed_high_f == null) {
        return tdisp(rec.forecast_high_f) + ' / ' + tdisp(rec.forecast_low_f) + ' · so far ' + tdisp(TL.observed_high_f) + ' / ' + tdisp(TL.observed_low_f);
      }
      return pair(rec, 'forecast_high_f', 'observed_high_f') + ' / ' + pair(rec, 'forecast_low_f', 'observed_low_f');
    }
    function rec(fn) { return function (r) { return r ? fn(r) : '—'; }; }
    function fixedTimes(r) { return hm(r.window_open_time) + '–' + hm(r.window_close_time) + ' (fixed for a ' + (r.day_type || '?') + ' day)'; }
    // Issue #1475: a record's flag means "recommended at any point that day" (compliance credit); today also says
    // what holds now, and names the plan's own periods instead of the day type's fixed times
    function advised(r) {
      if (r !== t || TL.windows_recommended_now == null) return yn(r.windows_recommended) + (r.windows_recommended ? ' · ' + fixedTimes(r) : '');
      var now = TL.windows_recommended_now, plan = TL.plan_windows || [];
      var head = now ? 'yes' : r.windows_recommended ? 'earlier today, not now' + (TL.forecast_windows_recommended ? ' (the live outdoor check says no)' : '') : 'no';
      var times = plan.length ? ' · plan ' + plan.map(function (w) { return hm(w[0]) + '–' + hm(w[1]); }).join(', ') : (now ? ' · ' + fixedTimes(r) : '');
      return head + times;
    }
    function opened(r) {
      if (!r.windows_physically_opened && !r.window_physical_open_time) return 'no';
      var credit = r.windows_recommended ? (r.windows_opened ? ' · counted as following advice' : ' · not counted (advice was off when opened)') : '';
      return 'yes · ' + span(r.window_physical_open_time, r.window_physical_close_time) + credit;
    }
    function runtime(r) {
      var tot = r.hvac_runtime_minutes || 0, h = r.hvac_heat_runtime_minutes, c = r.hvac_cool_runtime_minutes;
      if (h == null && c == null) return r0(tot) + ' min';
      // the split is recorded from 0.9.x on; a day that began on an older version has minutes only in the total
      return r0(tot) + ' min' + (h + c >= tot - 1 ? ' (heat ' + r0(h) + ' · cool ' + r0(c) + ')' : ' (heat/cool split not recorded for the whole day)');
    }
    var rows = [
      ['Day type · trend', rec(function (r) { return (r.day_type || '—') + ' · ' + (r.trend_direction || '—'); }),
        function (p) { return p ? (p.day_type || '—') : ''; }],
      ['Recommended HVAC mode', rec(function (r) { return r.hvac_mode_recommended || '—'; }), function (p) { return p ? (p.hvac_mode || '—') : ''; },
        'The mode today\u2019s classification recommends. Heating or cooling can still run on an "off" day to hold your comfort range (see HVAC runtime)'],
      ['High / low (forecast → observed)', rec(hl),
        function (p) { return p && (p.expected_high != null || p.expected_low != null) ? 'expected ' + ND.num(p.expected_high, 0) + u + ' / ' + ND.num(p.expected_low, 0) + u : ''; }],
      ['Windows advised', rec(advised),
        function (p) { return p ? yn(p.windows_recommended) + (p.windows_recommended ? ' · plan ' + hm(p.window_open_time) + '–' + hm(p.window_close_time) : '') : ''; },
        'Yes when opening windows was recommended at any point that day (that is what window advice is scored on). Fixed times come from the day type; plan times are the periods the plan holds'],
      ['Windows actually open', rec(opened), function () { return ''; },
        'When a door or window sensor read open. Counted as following advice only when it opened while windows were recommended'],
      ['HVAC runtime', rec(runtime), function () { return ''; }],
      ['Fan-only runtime', rec(function (r) { return r0(r.thermostat_fan_only_runtime_minutes) + ' min'; }), function () { return ''; }],
      ['Avg indoor · minutes outside comfort', rec(function (r) {
        var m = r.comfort_violations_minutes || 0;
        var avg = r.avg_indoor_temp != null ? tdisp(r.avg_indoor_temp, 1) + u : r === t && TL.avg_indoor_f != null ? tdisp(TL.avg_indoor_f, 1) + u + ' so far' : '—';
        return avg + ' · ' + Math.round(m) + ' min (' + (m / 1440 * 100).toFixed(0) + '% of day)';
      }), function () { return ''; }, tip],
      ['Night setback', rec(function (r) {
        if (r.setback_depth_f) return ND.num(ND.dFToDisp(r.setback_depth_f), 1) + u + (r.setback_was_adaptive ? ' (adaptive)' : '');
        if (r.setback_skipped_reason) return 'skipped (' + String(r.setback_skipped_reason).replace(/_/g, ' ') + ')';
        // Issue #1474: bedtime results were not recorded before 0.9.16
        return r === t ? 'after bedtime' : 'not recorded';
      }), function () { return ''; }],
      ['Pre-conditioning', function (r) { return r && r.pre_condition != null ? yn(r.pre_condition) : r ? 'not recorded' : '—'; }, function (p) { return p ? yn(p.pre_condition) : ''; },
        'Past days do not record whether the plan pre-heated or pre-cooled; the plan column does'],
      ['Manual changes · door/window pauses', rec(function (r) { return (r.manual_overrides || 0) + ' · ' + (r.door_window_pause_events || 0); }), function () { return ''; }],
      ['Away minutes', rec(function (r) { return r0(r.occupancy_away_minutes); }), function () { return ''; }],
      // Filled at end of day from the optional Settings > Advanced energy rate and heating/cooling power (#1230 R5c);
      // blank ("—") when any is unset. Tomorrow stays blank: the plan carries no predicted HVAC minutes yet.
      ['Estimated cost', rec(function (r) {
        if (r.estimated_cost != null) return Number(r.estimated_cost).toFixed(2);
        if (!L.cost_configured) return 'set an energy rate in Settings';
        return r === t ? 'after the day ends' : '—';
      }), function () { return ''; },
        'Energy cost of the heating and cooling that ran, from your energy rate and heating/cooling power settings. Shown after the day ends, and only when those are set.']
    ];
    var dt = function (r, p) { return r && r.date ? ' ' + String(r.date).slice(5) : p || ''; };
    var h = '<table style="min-width:640px"><thead><tr><th scope="col"><span class="sr-only">Measure</span></th><th scope="col">Yesterday' + esc(dt(y)) +
      '</th><th scope="col">Today' + esc(dt(t)) + '</th><th scope="col">Tomorrow (plan)' + esc(dt(n)) + '</th></tr></thead><tbody>';
    rows.forEach(function (r) {
      h += '<tr><th scope="row" class="k" style="font-weight:400"' + (r[3] ? ' title="' + esc(r[3]) + '"' : '') + '>' + esc(r[0]) + '</th><td class="num">' + esc(r[1](y)) +
        '</td><td class="num">' + esc(r[1](t) === '—' && !t ? 'No record yet today' : r[1](t)) + '</td><td class="num">' + esc(r[2](n)) + '</td></tr>';
    });
    host.innerHTML = h + '</tbody></table>';
    $('an-days-sub').textContent = 'Tomorrow shows what the plan knows today; blank cells fill in as the plan grows.';
    var c = L.compliance || {};
    $('an-compliance').textContent = c.status === 'active' ? 'Over the last ' + (c.window_days || Math.min(14, c.days_recorded)) + ' of ' + c.days_recorded + ' recorded days: window compliance ' + pct(c.window_compliance) +
      ', average HVAC runtime ' + Math.round(c.avg_daily_hvac_runtime_minutes || 0) + ' min/day, average fan-only ' + Math.round(c.avg_daily_thermostat_fan_only_runtime_minutes || 0) +
      ' min/day, comfort score ' + pct(c.comfort_score) + ', ' + (c.total_manual_overrides || 0) + ' manual overrides.' : 'Compliance summary appears once a few days are recorded.';
  }

  // Issues #1512/#1516: the cycle model's rows (engines payload ``decisions`` entries with source "cycle"), by mode.
  // A row carries its fields either on itself or in an object ``value``; mode comes from ``mode`` or the key.
  // q and sd_q arrive in the user's unit like every decisions row; coast_area_f is a Fahrenheit delta, converted here.
  var GRADE_WORDS = { none: 'not enough yet', low: 'rough', medium: 'fairly sure', high: 'sure' };
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
    if (!E && !tm) { err(host, D.errs[1] || 'no data'); return; }
    var rows = [], eu = E ? ND.sym(E.unit) : ND.sym(ND.unit);
    function row(plain, raw, conf) { rows.push('<div class="kv"><div>' + esc(plain) + '</div><div class="conf">' + esc(conf || '') + '</div><div class="raw">' + esc(raw) + '</div></div>'); }
    if (E) {
      var dr = {};
      (E.decisions || []).forEach(function (r) { if (r && typeof r.value === 'number') dr[r.key] = r; });
      var kp = E.k_passive && E.k_passive.active ? E.k_passive : null, hv = E.k_active_hvac && E.k_active_hvac.active ? E.k_active_hvac : null;
      var ks = E.k_solar && E.k_solar.active ? E.k_solar : null, kw = E.k_vent_window && E.k_vent_window.active ? E.k_vent_window : null, kf = E.k_vent_fan && E.k_vent_fan.active ? E.k_vent_fan : null;
      if (kp) row('Holds heat ' + (Math.abs(kp.value) < 0.05 ? 'well' : 'moderately') + ': it closes half the indoor–outdoor gap in about ' + Math.round(Math.log(2) / Math.abs(kp.value)) + ' hours',
        'k_passive ' + Number(kp.value).toFixed(4) + ' /h · time constant ' + Math.round(1 / Math.abs(kp.value)) + ' h', 'calibration: ' + (kp.confidence || 'n/a'));
      if (hv && hv.value) row('Heating adds about ' + Number(hv.value.heat).toFixed(1) + eu + ' an hour; cooling removes ' + Math.abs(hv.value.cool).toFixed(1) + eu,
        'k_active heat ' + Number(hv.value.heat).toFixed(2) + ' · cool ' + Number(hv.value.cool).toFixed(2) + ' ' + eu + '/h', 'calibration: ' + (hv.confidence || 'n/a'));
      // Issues #1512/#1516: the cycle model's measured heating and cooling rate, how sure it is, and the coast after a run.
      // Rows come from the engines payload (``decisions`` rows with source "cycle"); on a v4 zone decisions keep v4's rate.
      var cyc = cycleRows(E.decisions);
      // owner decision 2026-11-13: a thermostat that never reports heating/cooling: no runtime, no rates measured
      (E.decisions || []).forEach(function (r) { if (r && r.source === 'notice' && r.text) row(r.text, 'rate in use: ' + (r.rate_in_use || 'n/a'), ''); });
      ['heat', 'cool'].forEach(function (m) {
        var c = cyc[m]; if (!c || typeof c.q !== 'number') return;
        var word = m === 'heat' ? 'Heating' : 'Cooling';
        var line = word + ' measures ' + Math.abs(c.q).toFixed(1) + eu + ' an hour (' + GRADE_WORDS[c.grade || 'none'] + ')';
        if (typeof c.coast_area_f === 'number' && Math.abs(c.coast_area_f) >= 0.05)
          line += '; after a run the room ' + (c.coast_area_f * c.q > 0 ? 'keeps ' + (m === 'heat' ? 'warming' : 'cooling') + ' about ' : 'gives back about ') + Math.abs(ND.dFToDisp(c.coast_area_f)).toFixed(1) + eu;
        var cd = dr['k_active_' + m], onV4 = cd && cd.source && cd.source !== 'v3' && cd.source !== 'cycle' && typeof cd.value === 'number';
        if (onV4) line += '. Decisions use ' + cd.source + '\u2019s ' + Math.abs(cd.value).toFixed(1) + eu + '; the cycle model measures ' + Math.abs(c.q).toFixed(1) + eu;
        row(line, 'cycle q ' + Number(c.q).toFixed(2) + (typeof c.sd_q === 'number' ? ' \u00b1 ' + Number(c.sd_q).toFixed(2) : '') + ' ' + eu + '/h \u00b7 coast ' +
          (typeof c.coast_area_f === 'number' ? Number(ND.dFToDisp(c.coast_area_f)).toFixed(2) : '\u2014') + ' ' + eu + ' \u00b7 ' + (c.ne != null ? c.ne : 0) + ' informative cycles', 'sure: ' + (c.grade || 'none'));
        // Issue #1512 reopened: a whole- or half-degree sensor cannot learn the rate from normal cycles; say so plainly.
        if (c.coarse_sensor && c.coarse_sensor.text) row(c.coarse_sensor.text, 'sensor reports ' + (c.coarse_sensor.label || 'coarse steps') + ' \u00b7 rate in use: ' + (c.coarse_sensor.rate_in_use || 'n/a'), '');
      });
      if (ks) {
        var ph = E.solar_phase_offset_h && E.solar_phase_offset_h.active ? E.solar_phase_offset_h.value : 0;
        var peak = new Date(2000, 0, 1, 13 + Math.floor(ph), Math.round((ph % 1) * 60)).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
        row('Sun adds up to ' + Number(ks.value).toFixed(1) + eu + ' an hour, strongest around ' + peak, 'k_solar ' + Number(ks.value).toFixed(3) + ' · phase +' + Number(ph).toFixed(2) + ' h', 'calibration: ' + (ks.confidence || 'n/a'));
      }
      // Issue #1505: the window and fan line uses the rates the decisions and the timeline read (``decisions``: v4's own
      // on a v4 zone, v3's learner otherwise), all three from that one source so the ratios compare like with like.
      var dp = dr.k_passive || (kp ? { value: kp.value, source: 'v3' } : null);
      var dw = dr.k_vent_window || (kw ? { value: kw.value, source: 'v3' } : null), df = dr.k_vent_fan || (kf ? { value: kf.value, source: 'v3' } : null);
      if (dp && dp.value && (dw || df)) {
        var x = function (r) { return (Math.abs(r.value) / Math.abs(dp.value)).toFixed(1) + '×'; };
        var parts = [];
        if (dw) parts.push('with windows open it moves toward outdoors ' + x(dw) + ' as fast as closed up');
        if (df) parts.push('with the fan running, ' + x(df));
        var txt = parts.join('; ');
        row(txt.charAt(0).toUpperCase() + txt.slice(1),
          (dw ? 'k_vent_window ' + Number(dw.value).toFixed(3) : 'k_vent_window not learned') + ' · ' + (df ? 'k_vent_fan ' + Number(df.value).toFixed(3) : 'k_vent_fan not learned') +
          ' · closed ' + Number(dp.value).toFixed(3) + ' /h', 'from ' + (dw || df).source);
      }
      var s = $('an-model-status');
      s.textContent = E.physics_eligible ? 'Predictions use the physics model' : 'Predictions use the schedule fallback';
    }
    if (tm) {
      var sh = tm.swing_heat != null ? tm.swing_heat : null, sc = tm.swing_cool != null ? tm.swing_cool : null;
      var swSym = ND.sym(tm.unit || ND.unit), swDec = swSym === '°C' ? 2 : 1;  // Issue #1477: 1.5 F = 0.83 C, not 0.8
      // Issue #1477 owner decision: the rounded-up swing (detectors) with the measured one (the bedroom loop) beside it
      var swTxt = function (v, raw, measured) {
        var s = ND.num(v != null ? v : 1.5, swDec) + ' ' + swSym;
        return measured && raw != null && Math.abs(raw - v) > 0.004 ? s + ' (measured ' + ND.num(raw, 2) + ' ' + swSym + ')' : s;
      };
      if (sh != null || sc != null) row('Thermostat swing: heat starts ' + swTxt(sh, tm.swing_heat_raw, tm.swing_heat_measured) + ' below the setpoint, cooling ' + swTxt(sc, tm.swing_cool_raw, tm.swing_cool_measured) + ' above' +
        (tm.swing_heat_measured && tm.swing_cool_measured ? '' : ' (partly estimated)'), 'swing_heat ' + ND.num(sh, 2) + ' · swing_cool ' + ND.num(sc, 2) + ' ' + ND.sym(tm.unit || ND.unit), '');
      var envN = (tm.observation_count_passive || 0) + (tm.observation_count_vent_window || 0) + (tm.observation_count_vent_fan || 0) + (tm.observation_count_solar || 0);
      var hvN = (tm.observation_count_heat || 0) + (tm.observation_count_cool || 0);
      // Issue #1512: hvN is the evidence behind the heating/cooling rates (older measurements give way to new cycles,
      // never to time alone), not a running total
      row('Measurements kept: ' + envN + ' for the house; evidence behind the heating and cooling rates: ' + hvN + ' measurements', 'calibration: house ' + (tm.confidence_k_passive || 'none') + ' · HVAC ' + (tm.confidence || 'none'),
        tm.last_observation_date ? 'last: ' + new Date(tm.last_observation_date).toLocaleDateString() : 'none yet');
    }
    if (!rows.length) rows.push('<div class="empty">Nothing learned yet. It needs a few hours of HVAC-off time with a clear indoor-outdoor difference.</div>');
    var health = tm && tm.learning_health, html = rows.join('');
    if (health) {
      var tot = 0, lines = [];
      Object.keys(OBS).forEach(function (k) {
        var h = health[k]; if (!h || !h.attempts) return;
        var rej = Object.keys(h.rejections || {}).reduce(function (a, c) { return a + h.rejections[c]; }, 0); tot += rej;
        var last = h.last_rejection && h.last_rejection.reason_code ? ' · last: ' + (REASONS[h.last_rejection.reason_code] || h.last_rejection.reason_code) : '';
        lines.push('<div style="display:flex;justify-content:space-between;gap:8px"><span>' + esc(OBS[k]) + '</span><span>' + h.committed + ' kept, ' + rej + ' skipped' + esc(last) + '</span></div>');
      });
      if (lines.length) html += '<details><summary>Measurements skipped by type (' + tot + ')</summary><div class="small" style="display:grid;gap:4px;margin-top:6px">' + lines.join('') + '</div></details>';
    }
    host.innerHTML = html;
  }

  // ---------- control quality, prediction error, promises ----------
  function renderControl() {
    var q = cq24(), host = $('an-cq'), pe = $('an-pe');
    if (!q) {
      host.innerHTML = '<div class="empty">' + (D.errs[2] ? esc(D.errs[2]) : 'Not enough history yet.') + '</div>'; pe.innerHTML = '<div class="empty">Not available yet.</div>';
    } else {
      var hr = q.runtime.heating, cl = q.runtime.cooling;
      var items = [
        ['Heating', Math.round(hr.minutes) + ' min in ' + hr.runs + ' runs'], ['Median run · short runs', (hr.median_run_minutes != null ? hr.median_run_minutes : '—') + ' min · ' + hr.short_runs_lt_10m],
        ['Cooling', Math.round(cl.minutes) + ' min'], ['Mode changes', String(q.mode_changes.count)], ['Wrong edge armed', q.misarmed.minutes + ' min'],
        ['Heat while house was losing heat', Math.round(q.run_context.heating_minutes_house_losing_heat) + ' min'], ['Could have deferred', q.would_have_deferred.heating_minutes + ' min heat'],
        ['Settling-guard holds', String(q.settling_guard.hold_episodes)]
      ];
      host.innerHTML = '<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:6px 16px;font-size:13px">' + items.map(function (i) {
        return '<div style="display:flex;justify-content:space-between;gap:8px;border-top:1px solid var(--rule);padding-top:5px"><span style="color:var(--ink2)">' + esc(i[0]) + '</span><span class="num">' + esc(i[1]) + '</span></div>';
      }).join('') + '</div>';
      // Issue #1475: these bars score the forecast v3 logs (pred_indoor); say so when v3 is not the main engine
      var prim = (D.acc && D.acc.primary_engine) || (D.engines && D.engines.v4 && D.engines.v4.prediction_engines && D.engines.v4.prediction_engines.primary);
      $('an-pe-src').textContent = prim && prim !== 'v3' ? 'v3\u2019s logged forecast (the comparison engine); the main ' + prim + ' miss is in the headline' : '';
      var H = q.prediction_error.horizons, rowsPe = [['30 min', H['30m']], ['60 min', H['60m']], ['2 h', H['120m']], ['4 h', q.prediction_error.archive_lead]].filter(function (r) { return r[1]; });
      var mx = Math.max.apply(null, rowsPe.map(function (r) { return r[1].mean_abs_f; }).concat([1]));
      pe.innerHTML = rowsPe.map(function (r) {
        var v = r[1];
        return '<div style="display:grid;grid-template-columns:70px minmax(0,1fr) 110px;gap:8px;align-items:center;font-size:12px;margin-bottom:4px"><span style="color:var(--ink2)">' + r[0] +
          '</span><div style="height:10px;background:var(--chip);border-radius:3px"><div style="height:10px;border-radius:3px;background:var(--s2);width:' + Math.min(100, v.mean_abs_f / mx * 100) + '%"></div></div><span class="num">' +
          ND.num(ND.dFToDisp(v.mean_abs_f), 1) + '° (' + (v.mean_signed_f > 0 ? '+' : '') + ND.num(ND.dFToDisp(v.mean_signed_f), 1) + ')</span></div>';
      }).join('');
    }
    var ps = ND.status && ND.status.promise_summary, pr = $('an-promises');
    pr.innerHTML = ps && ps.promises && ps.promises.length ? ps.promises.map(function (p) {
      return '<div style="font-size:13px;display:flex;justify-content:space-between;gap:8px;border-top:1px solid var(--rule);padding-top:5px;margin-bottom:5px"><span>' +
        esc(String(p.label || p.promise).replace(/^(\d+): /, '#$1 ')) + '</span><span style="color:var(--ink2)">×' + p.hits + (p.last_hit ? ' · last ' + esc(ND.fmtDateTime(p.last_hit)) : '') + '</span></div>';
    }).join('') : '<div class="empty">None missed in the last 30 days.</div>';
  }

  // ---------- suggestions (respond_suggestion, exactly as classic) ----------
  function renderSuggestions() {
    var host = $('an-suggestions'), L = D.learning;
    if (!L) { err(host, D.errs[0] || 'no data'); return; }
    var list = L.suggestions || [];
    if (!list.length) { host.innerHTML = '<div class="empty">No suggestions right now. CA proposes setting changes here when a pattern repeats (for example, comfort misses on 5+ days).</div>'; return; }
    host.innerHTML = list.map(function (s) {
      var k = esc(s.key);
      return '<div class="kv" id="sug-' + k + '" style="grid-template-columns:1fr"><div>' + esc(s.text) + '</div><div class="row" id="sug-actions-' + k + '">' +
        '<button type="button" class="btn small primary" data-sug="' + k + '" data-act="accept">Accept</button>' +
        '<button type="button" class="btn small" data-sug="' + k + '" data-act="dismiss">Dismiss</button></div></div>';
    }).join('');
  }
  async function respond(key, action) {
    var box = $('sug-actions-' + key);
    try {
      await ND.api('respond_suggestion', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ action: action, suggestion_key: key }) });
      if (box) box.innerHTML = '<span class="badge ok">' + (action === 'accept' ? 'Accepted' : 'Dismissed') + '</span> <span class="small">Was this diagnosis accurate?</span> ' +
        '<button type="button" class="btn small" data-sug="' + esc(key) + '" data-fb="correct">Correct</button> <button type="button" class="btn small" data-sug="' + esc(key) + '" data-fb="incorrect">Incorrect</button>';
      setTimeout(function () { if (D.learning) { D.learning.suggestions = (D.learning.suggestions || []).filter(function (s) { return s.key !== key; }); renderSuggestions(); } }, 8000);
    } catch (e) { ND.toast(e.message, 'err'); }
  }
  async function feedback(key, verdict) {
    var box = $('sug-actions-' + key);
    try {
      await ND.api('respond_suggestion', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ feedback: verdict, suggestion_key: key }) });
      if (box) box.innerHTML = '<span class="badge ok">Feedback recorded</span>';
      setTimeout(function () { if (D.learning) { D.learning.suggestions = (D.learning.suggestions || []).filter(function (s) { return s.key !== key; }); renderSuggestions(); } }, 1500);
    } catch (e) { ND.toast(e.message, 'err'); }
  }

  // ---------- AI status line ----------
  function renderAiLine() {
    var a = D.ai && D.ai.status, el = $('an-ai-line');
    if (!a) { el.textContent = D.errs[4] ? 'AI status unavailable: ' + D.errs[4] : ''; return; }
    aiDisabled = a.status === 'disabled' || !a.status;
    el.textContent = 'AI ' + (a.status || 'unknown') + (a.model ? ' · ' + a.model : '') + (a.investigator_requests_limit != null ? ' · investigations today ' + (a.investigator_requests_today || 0) + '/' + a.investigator_requests_limit : '') +
      ' · $' + (a.monthly_cost_estimate || 0).toFixed(2) + ' this month · ' + (a.error_count || 0) + ' errors' + (a.circuit_breaker_state && a.circuit_breaker_state !== 'closed' ? ' · circuit ' + a.circuit_breaker_state : '');
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
      tw.innerHTML = '<option value="6">Last 6 hours</option><option value="12" selected>Last 12 hours</option><option value="24">Last 24 hours</option><option value="48">Last 48 hours</option><option value="168">Last 7 days</option>';
      $('investigation-focus-controls').hidden = true; $('run-report-btn').textContent = 'Generate';
    } else {
      tw.innerHTML = '<option value="24" selected>Last 1 day</option><option value="168">Last 7 days</option><option value="720">Last 30 days</option>';
      $('investigation-focus-controls').hidden = false; $('run-report-btn').textContent = 'Investigate';
    }
    applyAiState();
  }
  function reset(label) { var b = $('run-report-btn'); b.disabled = false; b.textContent = label; $('report-loading').textContent = 'Generating...'; $('report-loading').hidden = true; applyAiState(); }
  async function runActivity() {
    var hours = parseInt($('report-time-window').value, 10) || 12, btn = $('run-report-btn');
    btn.disabled = true; btn.textContent = 'Generating...'; $('report-loading').hidden = false; $('report-status').textContent = '';
    try {
      var resp = await ND.api('activity_record?hours=' + hours);
      activityTable = resp.table || '';
      $('report-content').innerHTML = activityTable ? '<div>' + ND.md(activityTable) + '</div>' : '<div class="empty">No events in the selected window.</div>';
      $('report-actions').hidden = false;
    } catch (e) { $('report-status').textContent = 'Error: ' + e.message; } finally { reset('Generate'); }
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
  function renderProgress(content, acc) {
    var p = parseSections(acc);
    if (!p.order.length) { content.innerHTML = '<div class="empty">Writing investigation...</div>'; return; }
    content.innerHTML = p.order.map(function (k) {
      var def = SECTIONS.filter(function (d) { return d.key === k; })[0]; if (!def) return '';
      return '<details open><summary><strong>' + esc(def.label) + '</strong>' + (k === p.active ? ' <span class="sub">writing...</span>' : '') + '</summary><div class="note">' + ND.md(p.sections[k] || '') + '</div></details>';
    }).join('');
  }
  async function runInvestigation() {
    var btn = $('run-report-btn'), loading = $('report-loading'), status = $('report-status'), content = $('report-content');
    var focus = $('investigation-focus').value.trim(), hours = parseInt($('report-time-window').value, 10) || 24, deep = $('investigation-deep').checked;
    btn.disabled = true; btn.textContent = 'Running...'; loading.hidden = false; loading.textContent = 'Connecting...'; status.textContent = ''; content.innerHTML = ''; $('report-actions').hidden = true;
    var acc = '';
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
          if (ev.type === 'thinking') loading.textContent = 'Thinking... ' + ev.text.replace(/[\n]+/g, ' ').trim().slice(-180);
          else if (ev.type === 'status') loading.textContent = ev.text;
          else if (ev.type === 'chunk') { var first = !acc.length; acc += ev.text; renderProgress(content, acc); if (first) loading.hidden = true; status.textContent = 'Receiving response... (' + acc.length + ' chars)'; }
          else if (ev.type === 'done') {
            var result = { success: ev.success !== false, source: ev.source || 'ai', data: ev.data || {}, error: ev.error || null, raw_response: ev.raw_response || acc, input_context: ev.input_context || '', truncated: !!ev.truncated, truncated_empty: !!ev.truncated_empty };
            currentReport = { reportType: 'investigation', timestamp: new Date().toISOString(), result: result, focus: focus };
            renderReport(currentReport);
            status.textContent = result.truncated_empty ? 'The model produced no visible output despite using the full response budget. Try a different reasoning effort or model.'
              : result.truncated ? 'Report truncated: it hit the max response length. Increase "Investigator Max Response Length" in AI settings and re-run.'
              : 'Completed at ' + new Date().toLocaleTimeString() + ' (source: ' + result.source + ')';
            loadHistory();
          } else if (ev.type === 'error') throw new Error(ev.message || 'Investigation failed');
        }
      }
      if (!currentReport && acc) { content.innerHTML = '<pre style="white-space:pre-wrap;font-size:12px">' + esc(acc) + '</pre>'; status.textContent = 'Stream ended (no structured response)'; }
    } catch (e) {
      status.textContent = 'Error: ' + e.message + (e.message.toLowerCase().indexOf('not enabled') >= 0 ? ' - enable it in Home Assistant, Settings, Integrations, Climate Advisor, Configure, AI settings' : '');
      content.innerHTML = '';
    } finally { reset('Investigate'); }
  }
  function renderReport(entry) {
    var content = $('report-content'), res = entry.result || {}, ts = entry.timestamp ? new Date(entry.timestamp).toLocaleString() : '';
    if (!res.success) { content.innerHTML = '<div class="err">Report failed (' + esc(res.source || 'error') + '): ' + esc(res.error || 'Unknown error') + '</div>'; $('report-actions').hidden = true; return; }
    var data = res.data || {}, h = (ts ? '<div class="sub">' + esc(ts) + (res.is_refined ? ' <span class="badge">refined</span>' : '') + '</div>' : '');
    if (res.truncated_empty) h += '<div class="banner warn">The model produced no visible output despite using the full response budget.</div>';
    else if (res.truncated) h += '<div class="banner warn">Report truncated: it hit the max response length.</div>';
    h += '<div class="small">' + (res.source === 'fallback' ? 'Fallback (AI unavailable)' : 'AI-generated') + '</div>';
    var any = false;
    SECTIONS.forEach(function (d) { var tx = data[d.key] || ''; if (!tx) return; any = true; h += '<details open><summary><strong>' + esc(d.label) + '</strong></summary><div class="note">' + ND.md(tx) + '</div></details>'; });
    if (!any && res.raw_response) h += '<div class="note">' + ND.md(res.raw_response) + '</div>';
    content.innerHTML = h; $('report-actions').hidden = false;
  }
  function formatReport() {
    if (!currentReport) return '';
    var ts = currentReport.timestamp ? new Date(currentReport.timestamp).toLocaleString() : 'Unknown time', res = currentReport.result || {}, data = res.data || {};
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
    if (!title) { st.textContent = 'Title is required.'; return; }
    st.textContent = 'Submitting...'; btn.disabled = true;
    body = await appendIncident(body);
    var token = await ND.getToken();
    try {
      var resp = await fetch('/api/climate_advisor/' + await ND.scopedPath('submit_github_issue'), { method: 'POST', headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + token }, body: JSON.stringify({ title: title, body: body, labels: labels }) });
      var data = await resp.json();
      if (data.success) { st.innerHTML = 'Issue created: <a href="' + esc(data.issue_url) + '" target="_blank" rel="noopener">#' + esc(data.issue_number) + '</a>'; $('gh-cancel').textContent = 'Close'; }
      else if (data.error === 'not_configured') { $('github-modal-not-configured').hidden = false; st.textContent = ''; btn.disabled = false; }
      else { st.textContent = 'Failed: ' + (data.error || 'unknown error'); btn.disabled = false; }
    } catch (e) { st.textContent = 'Network error: ' + e.message; btn.disabled = false; }
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
    } catch (e) { host.innerHTML = '<div class="empty">Failed to load history.</div>'; }
  }
  function renderHistory() {
    var host = $('unified-history-list'), f = $('hist-from-date').value, t = $('hist-to-date').value;
    var from = f ? new Date(f) : null, to = t ? new Date(t + 'T23:59:59') : null;
    filtered = allHistory.filter(function (e) {
      if (histFilter !== 'all' && (e.report_type || 'activity') !== histFilter) return false;
      var ts = new Date(e.timestamp); return !(from && ts < from) && !(to && ts > to);
    });
    if (!filtered.length) { host.innerHTML = '<div class="empty">' + (allHistory.length ? 'No reports match the current filter.' : 'No reports yet.') + '</div>'; return; }
    var shown = Math.min(histShowing, filtered.length);
    host.innerHTML = filtered.slice(0, shown).map(function (e, i) {
      var res = e.result || {}, sum = res.data && res.data.activity_summary ? res.data.activity_summary.replace(/\s+/g, ' ').trim() : '';
      if (sum.length > 110) sum = sum.slice(0, 110) + '...';
      return '<div class="hist-row"><div class="body"><div class="row" style="gap:6px"><strong style="font-size:13px">' + esc(e.timestamp ? new Date(e.timestamp).toLocaleString() : 'Unknown time') + '</strong>' +
        '<span class="badge">' + esc(e.report_type === 'investigation' ? 'Investigation' : 'Activity') + '</span>' + (res.truncated_empty ? '<span class="badge changed">no output</span>' : res.truncated ? '<span class="badge changed">truncated</span>' : '') +
        '<span class="sub">' + esc(res.source || 'unknown') + '</span></div>' + (sum ? '<div class="small">' + esc(sum) + '</div>' : '') + '</div>' +
        '<div class="acts"><button type="button" class="btn small" data-h="open" data-i="' + i + '">Open</button><button type="button" class="btn small" data-h="copy" data-i="' + i + '">Copy</button>' +
        '<button type="button" class="btn small" data-h="issue" data-i="' + i + '">Issue</button><button type="button" class="btn small danger" data-h="del" data-i="' + i + '" aria-label="Delete report">Delete</button></div></div>';
    }).join('') + (filtered.length > shown ? '<button type="button" class="btn small" id="hist-more" style="margin-top:8px">Show more (' + (filtered.length - shown) + ' remaining)</button>' : '');
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
        if (currentReport && currentReport.timestamp === e.timestamp) { currentReport = null; $('report-content').innerHTML = '<div class="empty">Report deleted.</div>'; $('report-actions').hidden = true; }
        D.reports = null; await loadHistory();
      } catch (er) { ND.toast('Failed to delete report: ' + er.message, 'err'); }
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
    if (!PA) { $('an-pa-view').innerHTML = '<div class="err">Failed to load the accuracy view.</div>'; return; }
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
