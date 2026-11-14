/* Issue #1230 R5: Debug tab - every internal field. Reads automation_state, engines, event_log (diagnostics) and
   prediction_accuracy. Every automation_state key is shown exactly once in a named group, in words (never raw data),
   except promise_summary (shown in Analysis) and any key listed in DOWNLOAD_ONLY. There is no catch-all group:
   tests/test_new_dashboard.py fails when get_debug_state() gains a key that is in none of these lists (Issue #1399). */
(function () {
  'use strict';
  var T = window.CA_I18N.t;
  var $ = ND.$, esc = ND.esc;
  var A = null, E = null, EV = null, evOff = {}, loaded = false, timer = null;

  // Group -> automation_state keys. 'Today's classification', 'Learning pipeline', 'Prediction engines' and 'Last thermostat
  // command' are built from nested/other payloads below. Keep in sync with tests/test_new_dashboard.py.
  // Group titles are translated for display (T(...)); the English string is kept as the lookup/matching key everywhere
  // else in this file (buildGroups, the splice/push titles below), so GROUPS[i][0] itself must stay in English.
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
  var GROUP_TITLE_KEY = {
    'Control': 'dbg.g_control', 'Manual override & grace': 'dbg.g_override_grace', 'Doors, windows & pause': 'dbg.g_doors_pause',
    'Fan': 'dbg.g_fan', 'Natural ventilation & economizer': 'dbg.g_natvent', 'Comfort edge': 'dbg.g_comfort_edge',
    'Occupancy': 'dbg.g_occupancy', 'Units': 'dbg.g_units', 'Prediction engines': 'dbg.g_engines', 'Plan': 'dbg.g_plan',
    'Today’s classification': 'dbg.g_classification', 'Learning pipeline': 'dbg.g_learning_pipeline', 'Last thermostat command': 'dbg.g_last_command'
  };
  function gTitle(title) { return GROUP_TITLE_KEY[title] ? T(GROUP_TITLE_KEY[title]) : title; }
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
    occupancy_mode: 'Guest > Vacation > Home/Away',
    pre_pause_mode: 'The thermostat mode when the door or window opened, restored when it closes. It can differ from today\u2019s classification (the comfort edge or a manual change may have set it later)',
    pause_suppressed_classification: 'Today\u2019s plan is on hold while a door or window is open; it is applied again when they close',
    manual_grace_duration: 'How long Climate Advisor leaves a manual thermostat change alone',
    fan_remote_speed: 'The fan remote\u2019s speed setting, shown even while the fan is off',
    'Training-fit miss': 'How closely the last fit matched its own training history, not live accuracy: the Backtest below scores live predictions',
    'Grade reason': 'v4 grades none / low / ok from its history length, its skill against \u201cno change\u201d, and usable heating and cooling thermostat starts. It is not an accuracy score',
    'By mode': 'Each mode graded on its own usable thermostat starts and how well v4 predicts the hours it runs. Shown for information: the decisions read the whole-model grade, which needs every mode the home has used to be ok',
    'Shadow engine': 'v3 grades from how many observations it has learned, not from how accurate it is: compare both in the Backtest below',
    Archive: 'Saved forecasts the Backtest scores against what actually happened',
    'Last prediction': 'Slots archived = new forecast slots this prediction added to the archive (most predictions add none)',
    'Mass node': 'v4\u2019s estimate of the slow part of the house (walls, floors, furniture), which lags the air temperature',
    'Decision grade': 'The confidence the decisions read: v4\u2019s whole-model grade (low reads as low, ok as medium). The per-mode grades above are shown for information and do not change it',
    'v3 learned': 'v3\u2019s learned value from its observations, collected whichever engine is primary. The Decisions rows say which values the decisions actually read',
    'v3 model': 'v3\u2019s own model version and checks; it drives decisions only when v3 is the primary engine',
    comfort_family_armed: 'Which comfort edge the thermostat is guarding, and the last heat/cool switch',
    comfort_family_hold: 'The thermostat is deliberately not flipping heat/cool yet: the house is only slightly past an edge shortly after the other side ran',
    'classification.day_type': 'Drives HVAC strategy', 'classification.windows_recommended': 'Open windows instead of HVAC',
    'classification.pre_condition': 'Pre-heat/cool before peak', 'classification.setback_modifier': 'Trend-based setpoint adjust',
    'classification.today_high': 'The higher of the forecast high and the warmest reading so far today',
    'classification.low_is_estimated': 'Yes = the forecast gave no low; windows are then never recommended',
    'classification.forecast_windows_recommended': 'The forecast\u2019s answer before the live outdoor check'
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
  function dur(sec) {  // 10800 -> "3 h", 90 -> "1 min 30 s"
    var h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), x = Math.round(sec % 60);
    return [h ? h + ' h' : '', m ? m + ' min' : '', x || (!h && !m) ? x + ' s' : ''].filter(Boolean).join(' ');
  }
  function fmtKey(k, v) {
    if (typeof v === 'number' && /(_duration|_seconds|_seconds_remaining)$/.test(k)) return dur(v);
    if (k === 'fan_remote_speed' && has(v) && A && A.fan_active === false) return v + ' (fan is off)';
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
    // Issue #1469: v4 grades heating and cooling apart; the row names both (a mode the home has not used: "not used")
    // Issue #1525: a mode whose learned values are carried from last season reads "(carried)"
    var md = (v && v.modes) || {}, modeG = function (m, w) { var g = md[m] && md[m].grade; return g ? w + ' ' + (g === 'unseen' ? 'not used' : g) + (md[m].carried_grade ? ' (carried)' : '') : ''; };
    var perMode = [modeG('heat', 'heating'), modeG('cool', 'cooling')].filter(Boolean).join(' · ');
    rows.push(['Primary engine', !pe || !pe.primary ? DASH : !prim ? pe.primary : prim.label + ': ' + (prim.usable ? (prim.id === 'v4' && perMode ? 'usable · ' + perMode : ['usable', conf(prim)].filter(Boolean).join(', ')) : 'still learning')]);
    var gr = (v && v.grade) || {};
    if (prim && prim.id === 'v4' && (gr.why || gr.by_mode)) {
      if (gr.why) rows.push(['Grade reason', (v.confidence || 'none') + ': ' + gr.why]);
      if (gr.by_mode) rows.push(['By mode', gr.by_mode]);
    }
    // Issue #1529: an unusable refit never replaces the model in use; say so
    if (v && v.unusable_refit && v.unusable_refit.text) rows.push(['Last refit', v.unusable_refit.text + (pe && pe.shadow === 'v4' ? ' (v4 is the comparison engine)' : '')]);
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
    rows.push(['Training-fit miss', leads.length ? leads.map(function (k) { return k.replace(/^(\d+)\s*h$/, '$1 h') + ' ' + Number(mae[k]).toFixed(2) + u; }).join(' · ') : DASH]);
    var sens = Array.isArray(v.sensors) ? v.sensors : [], names = v.sensor_names || {};
    var sname = function (id) { return id === '_ca' ? 'Climate Advisor reading' : names[id] || id; };
    rows.push(['Sensors modelled', !sens.length ? DASH : sens.length === 1 && sens[0] === '_ca' ? 'Climate Advisor reading only' : sens.map(sname).join(', ')]);
    // Issue #1432: which model the decisions read (stitched CA reading or each room separately) and why, in words
    var fm = v.forecast_model && typeof v.forecast_model === 'object' ? v.forecast_model : null;
    rows.push(['Forecast model', fm && fm.text ? String(fm.text) : DASH]);
    var lc = v.last_cycle || {};
    rows.push(['Last prediction', !lc.at ? DASH : [tDay(lc.at), has(lc.inputs) ? count(lc.inputs, 'forecast input', 'forecast inputs') : '', has(lc.ms) ? Math.round(lc.ms) + ' ms' : '', has(lc.archived) ? count(lc.archived, 'new slot', 'new slots') + ' archived' : '', lc.predicted === false ? 'no prediction made' : ''].filter(Boolean).join(', ')]);
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

  // ---------- what the decisions read, and from where (engines payload's decisions list) ----------
  var DEC_LABEL = { confidence: 'Decision grade', k_passive: 'Decisions: heat-loss rate', k_active_heat: 'Decisions: heating rate',
    k_active_cool: 'Decisions: cooling rate', k_solar: 'Decisions: sun gain', solar_phase_offset_h: 'Decisions: sun timing',
    k_vent_window: 'Decisions: window venting', k_vent_fan: 'Decisions: fan venting' };
  function decisionRows(list, pe) {
    if (!Array.isArray(list) || !list.length) return [];
    var u = ND.sym(ND.unit), prim = (pe && pe.primary) || 'the primary engine';
    var from = { v3: 'from v3\u2019s learned observations', v4: 'from v4', default: 'default (' + prim + ' still learning)', none: 'not learned yet (decisions use their defaults)' };
    return list.filter(function (d) { return d && d.source !== 'cycle' && d.source !== 'notice'; }).map(function (d) {
      var val = d.value;
      if (d.source === 'none' || d.source === 'default') val = null;
      var txt = val == null ? '' : d.kind === 'rate' ? fmt(val) + ' ' + u + '/h' : d.kind === 'per_hour' ? fmt(val) + ' /h' : d.kind === 'hours' ? fmt(val) + ' h' : String(val);
      return { k: DEC_LABEL[d.key] || d.key, v: (txt ? txt + ' · ' : '') + (from[d.source] || d.source), hint: d.key === 'confidence' ? HINTS['Decision grade'] : 'The value the decisions read now (' + d.key + ')' };
    });
  }

  // Issues #1512/#1516: the cycle model's measurement (decisions rows with source "cycle"), shown beside what the decisions read.
  // Fields sit on the row or in an object ``value``; q and sd_q arrive in the user's unit, coast_area_f is a Fahrenheit delta.
  function cycleModelRows(list) {
    var u = ND.sym(ND.unit), rows = [];
    (Array.isArray(list) ? list : []).forEach(function (d) {
      if (!d || d.source !== 'cycle') return;
      var f = d.value && typeof d.value === 'object' ? d.value : d, m = d.mode || (String(d.key || '').indexOf('cool') >= 0 ? 'cool' : 'heat');
      if (typeof f.q !== 'number') return;
      var parts = [fmt(f.q) + (typeof f.sd_q === 'number' ? ' \u00b1 ' + fmt(f.sd_q) : '') + ' ' + u + '/h', 'grade ' + (f.grade || 'none')];
      if (typeof f.coast_area_f === 'number') parts.push('coast ' + fmt(ND.dFToDisp(f.coast_area_f)) + u);
      if (typeof f.k_p_cycle === 'number') parts.push('its own leak ' + fmt(f.k_p_cycle) + ' /h (diagnostic only)');
      if (f.ne != null) parts.push(f.ne + ' informative cycles' + (f.n_cycles != null ? ' of ' + f.n_cycles : ''));
      if (typeof f.resolution_f === 'number') parts.push('sensor steps ' + fmt(f.resolution_f) + '\u00b0F');
      if (typeof f.lag_min === 'number') parts.push('sensor lag ' + fmt(f.lag_min) + ' min' + (f.lag_identified ? '' : ' (not identified yet)'));
      if (Array.isArray(f.grade_caps) && f.grade_caps.length) parts.push('grade held down: ' + f.grade_caps.map(function (x) { return x === 'k_typical_prior' ? 'leak rate is only the typical value' : x === 'lag_unidentified' ? 'short runs, sensor lag not known yet' : x === 'imprecise' ? 'still too uncertain for its size' : x === 'contradicted' ? 'newer runs disagree with an older one' : x; }).join(', '));
      rows.push({ k: 'Cycle model: ' + (m === 'cool' ? 'cooling' : 'heating'), v: parts.join(' \u00b7 '),
        hint: 'What the pooled cycle model measures from every informative cycle. A v4 zone\u2019s decisions keep v4\u2019s own control rate; this is shown beside it.' });
      // Issue #1512 reopened: coarse-sensor notice (same text as Analysis and the AI context)
      if (f.coarse_sensor && f.coarse_sensor.text) rows.push({ k: 'Cycle model: ' + (m === 'cool' ? 'cooling' : 'heating') + ' sensor', v: f.coarse_sensor.text,
        hint: 'sensor_class ' + f.coarse_sensor.sensor_class + ', rate in use: ' + f.coarse_sensor.rate_in_use });
    });
    // owner decision 2026-11-13: a thermostat that never reports heating/cooling (same text as Analysis and the AI context)
    (Array.isArray(list) ? list : []).forEach(function (d) {
      if (d && d.source === 'notice' && d.text) rows.push({ k: 'Thermostat reports', v: d.text, hint: 'rate in use: ' + (d.rate_in_use || 'n/a') });
    });
    return rows;
  }

  // ---------- today's classification, in words, with the thresholds each row is judged on ----------
  function hm(t) {  // "10:00:00" -> "10:00 AM"
    var m = /^(\d\d):(\d\d)/.exec(t || ''); if (!m) return DASH;
    var d = new Date(); d.setHours(+m[1], +m[2], 0, 0); return tShort(d);
  }
  function classificationRows(c) {
    var u = ND.sym(ND.unit), r = c.window_rule || {}, th = c.trend_thresholds || {}, rows = [];
    var deg = function (x) { return has(x) ? ND.num(x, 1) + u : DASH; };
    var dd = function (f) { return ND.num(ND.dFToDisp(f), 1) + '°'; };
    function add(k, v, hint) { rows.push({ k: k, v: v, hint: hint || HINTS['classification.' + k] || '' }); }
    add('day_type', fmt(c.day_type));
    add('trend_direction', fmt(c.trend_direction));
    add('trend_magnitude', has(c.trend_magnitude) ? ND.num(c.trend_magnitude, 1) + '°' : DASH,
      has(th.moderate) ? 'The setback adjusts at ' + ND.num(th.moderate, 0) + '° (by ' + dd(2) + ') and ' + ND.num(th.significant, 0) + '° (by ' + dd(3) + ')' : '');
    add('hvac_mode', fmt(c.hvac_mode));
    var why = [];
    if (r.kind === 'range') {
      if (c.low_is_estimated) why.push('today\u2019s low is only an estimate');
      if (has(c.today_high) && has(r.high_at_least) && c.today_high < r.high_at_least) why.push('today\u2019s high ' + deg(c.today_high) + ' is below ' + deg(r.high_at_least));
      if (has(c.today_low) && has(r.low_at_most) && c.today_low > r.low_at_most) why.push('today\u2019s low ' + deg(c.today_low) + ' is above ' + deg(r.low_at_most));
      if (!why.length && c.forecast_windows_recommended && !c.windows_recommended) why.push('the forecast says yes, the live outdoor check says no');
    } else if (r.kind === 'opportunity') why.push('a hot day: see the morning and evening opportunities');
    else if (c.day_type) why.push('a ' + c.day_type + ' day: windows are not judged');
    add('windows_recommended', (c.windows_recommended ? 'yes' : 'no') + (why.length ? ' (' + why.join('; ') + ')' : ''),
      r.kind === 'range' ? 'Yes when today\u2019s high reaches ' + deg(r.high_at_least) + ' (comfort heat) and today\u2019s low reaches ' + deg(r.low_at_most) + ' (comfort cool minus 3°)' + (r.hysteresis ? ', with slack while already recommended' : '') : HINTS['classification.windows_recommended']);
    var fixed = c.day_type ? ' (fixed for a ' + c.day_type + ' day' + (c.windows_recommended ? ')' : '; windows not recommended today)') : '';
    add('window times', c.window_open_time ? hm(c.window_open_time) + ' – ' + hm(c.window_close_time) + fixed : 'none on a ' + (c.day_type || '?') + ' day',
      'The open and close times are set by the day type and also feed the pre-cool and natural-ventilation plans, so they show even when windows are not recommended');
    function opp(flag, s, e, name) {
      if (r.kind !== 'opportunity') return 'not checked (hot days only)';
      return flag ? 'yes, ' + hm(s) + ' – ' + hm(e) : 'no (' + name + ' low above ' + deg(r.opportunity_low_at_most) + ')';
    }
    add('morning opportunity', opp(c.window_opportunity_morning, c.window_opportunity_morning_start, c.window_opportunity_morning_end, 'today\u2019s'), 'On a hot day: needs today\u2019s low at or below ' + deg(r.opportunity_low_at_most));
    add('evening opportunity', opp(c.window_opportunity_evening, c.window_opportunity_evening_start, c.window_opportunity_evening_end, 'tomorrow\u2019s'), 'On a hot day: needs tomorrow\u2019s low at or below ' + deg(r.opportunity_low_at_most));
    add('pre_condition', c.pre_condition ? 'yes, ' + ND.num(c.pre_condition_target, 1) + '° past comfort' : 'no', HINTS['classification.pre_condition'] + ' (a cooling trend of ' + ND.num(th.moderate, 0) + '° or more)');
    add('setback_modifier', ND.num(c.setback_modifier || 0, 1) + '°', HINTS['classification.setback_modifier']);
    add('today_high', deg(c.today_high));
    add('today_low', deg(c.today_low), r.kind === 'range' ? 'Windows need today\u2019s low at or below ' + deg(r.low_at_most) : r.kind === 'opportunity' ? 'The morning opportunity needs it at or below ' + deg(r.opportunity_low_at_most) : 'Not used for windows on this day type');
    add('tomorrow_low', deg(c.tomorrow_low), r.kind === 'opportunity' ? 'The evening opportunity needs it at or below ' + deg(r.opportunity_low_at_most) : 'Used only on hot days (the evening opportunity)');
    add('low_is_estimated', fmt(c.low_is_estimated));
    add('forecast_windows_recommended', fmt(c.forecast_windows_recommended));
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
          rows = rows.concat(decisionRows(E.decisions, v4s && v4s.prediction_engines));
          rows = rows.concat(cycleModelRows(E.decisions));
          // v3's learned values (the learning observations): always collected, whichever engine is primary
          ['k_passive', 'k_solar', 'solar_phase_offset_h', 'k_vent_window', 'k_vent_fan'].forEach(function (k) {
            var e = E[k]; if (e) rows.push(row('v3 ' + k, e.active ? fmt(e.value) + (e.confidence ? ' (' + e.confidence + ')' : '') : '(not yet active)', 'v3 learned'));
          });
          var h = E.k_active_hvac; if (h) rows.push(row('v3 k_active_hvac', h.active && h.value ? 'heat ' + fmt(h.value.heat) + ' · cool ' + fmt(h.value.cool) + (h.confidence ? ' (' + h.confidence + ')' : '') : '(not yet active)', 'v3 learned'));
          rows.push(row('v3 model', (E.ode_version || '?') + ' physics · ' + (E.physics_eligible ? 'eligible' : 'not eligible') + (E.physics_eligible_reason ? ' · ' + E.physics_eligible_reason : '')));
        }
      }
      if (g[0] === 'Plan' && A && 'operation_timeline' in A) rows = rowsOf(planRows(A.operation_timeline));
      groups.push({ title: gTitle(g[0].indexOf('Prediction engines') === 0 ? 'Prediction engines' : g[0]), rows: rows });
    });
    if (A && A.classification) { groups.splice(8, 0, { title: gTitle('Today’s classification'), rows: classificationRows(A.classification) }); }
    if (A && A.thermal_pipeline) {
      var tp = A.thermal_pipeline;
      groups.push({ title: gTitle('Learning pipeline'), rows: (tp.pending || []).map(function (p) {
        return row('pending ' + p.obs_type, p.status + ' · ' + p.elapsed_minutes + ' min · ' + p.sample_count + ' samples · indoor Δ' + ND.num(ND.dFToDisp(p.indoor_delta_f), 2) + '° · outdoor ' + ND.num(ND.fToDisp(p.outdoor_f), 1) + ND.sym(ND.unit));
      }).concat(Object.keys(tp.rejection_log_counts || {}).map(function (k) {
        var n = tp.rejection_log_counts[k], cap = tp.rejection_log_cap;
        return { k: 'skipped ' + k, v: has(cap) && n >= cap ? cap + ' or more (the list keeps the newest ' + cap + ')' : String(n), hint: 'Observations of this type that were not learned from' };
      })) });
    }
    var lc = EV && EV.diagnostics && EV.diagnostics.last_command;
    if (lc) groups.push({ title: gTitle('Last thermostat command'), rows: Object.keys(lc).map(function (k) { return row(k, fmt(lc[k])); }) });
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
      html += '<section class="card" style="margin:0 0 10px"><h2 style="font-size:14px">' + esc(g.title) + ' <span class="sub">' + esc(T('dbg.n_fields', { n: rows.length })) + '</span></h2><table class="field-table"><tbody>' +
        rows.map(function (r) { return '<tr><td class="k"><span class="kn">' + esc(r.k) + '</span>' + (r.hint ? '<span class="hint">' + esc(r.hint) + '</span>' : '') + '</td><td class="mono">' + esc(r.v) + '</td></tr>'; }).join('') + '</tbody></table></section>';
    });
    host.innerHTML = html || '<div class="empty">' + esc(T('dbg.no_field_matches')) + '</div>';
    $('dbg-count').textContent = T('dbg.n_fields', { n: total });
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
    }).join('') : '<tr><td colspan="3" class="empty">' + esc(EV ? T('dbg.no_events_window') : T('dbg.event_log_unavailable')) + '</td></tr>';
    $('dbg-ev-count').textContent = T('dbg.last_n_of', { window: $('dbg-ev-hours').selectedOptions[0].textContent, shown: rows.length, total: evs.length });
    if (EV && EV.is_truncated) $('dbg-ev-count').textContent += ' · ' + T('dbg.older_not_kept');
  }

  async function loadEvents() {
    try { EV = await ND.api('event_log?hours=' + $('dbg-ev-hours').value); } catch (e) { EV = null; }
    render();
  }
  async function load() {
    var r = await Promise.allSettled([ND.api('automation_state'), ND.api('engines'), ND.api('event_log?hours=' + $('dbg-ev-hours').value)]);
    A = r[0].status === 'fulfilled' ? r[0].value : null; E = r[1].status === 'fulfilled' ? r[1].value : null; EV = r[2].status === 'fulfilled' ? r[2].value : null;
    loaded = true;
    if (!A) $('dbg-groups').innerHTML = '<div class="err">' + esc(T('dbg.failed_load', { msg: r[0].reason ? r[0].reason.message : T('set.no_data') })) + '</div>';
    else render();
    loadBacktest();
  }

  // ---------- downloads (same bundle as classic's Download Logs) ----------
  async function downloadLogs() {
    var b = $('dbg-dl-logs'), orig = b.textContent; b.textContent = T('dbg.downloading'); b.disabled = true;
    try {
      var r = await Promise.allSettled([ND.api('event_log?hours=168'), ND.api('investigation_reports')]);
      var bundle = { downloaded_at: new Date().toISOString(), events: r[0].status === 'fulfilled' ? (r[0].value.events || []) : [], diagnostics: r[0].status === 'fulfilled' ? (r[0].value.diagnostics || null) : null };
      if (r[1].status === 'fulfilled') bundle.investigation_reports = r[1].value;
      ND.download(JSON.stringify(bundle, null, 2), 'climate-advisor-logs-' + new Date().toISOString().slice(0, 10) + '.json', 'application/json');
    } catch (e) { ND.toast(T('dbg.failed_download_logs', { msg: e.message }), 'err'); } finally { b.textContent = orig; b.disabled = false; }
  }
  async function downloadDiagnostics() {
    try {
      var ev = await ND.api('event_log?hours=2');
      ND.download(JSON.stringify({ downloaded_at: new Date().toISOString(), diagnostics: ev.diagnostics || null, automation_state: A }, null, 2), 'climate-advisor-diagnostics-' + new Date().toISOString().slice(0, 10) + '.json', 'application/json');
    } catch (e) { ND.toast(T('dbg.failed_download_diag', { msg: e.message }), 'err'); }
  }

  // ---------- backtest lab ----------
  async function loadBacktest(lead, range) {
    var host = $('dbg-bt');
    lead = lead || (host.getAttribute('data-lead') || '4h'); range = range || (host.getAttribute('data-range') || '7d');
    host.setAttribute('data-lead', lead); host.setAttribute('data-range', range);
    var opt = function (list, cur) { return list.map(function (x) { return '<option' + (x === cur ? ' selected' : '') + '>' + x + '</option>'; }).join(''); };
    var head = '<div class="row"><label class="small">' + esc(T('dbg.look_ahead')) + ' <select id="bt-lead" class="ctl">' + opt(['1h', '4h', '12h', '24h'], lead) + '</select></label>' +
      '<label class="small">' + esc(T('dbg.range')) + ' <select id="bt-range" class="ctl">' + opt(['24h', '3d', '7d', '30d'], range) + '</select></label><button type="button" class="btn small" id="bt-run">' + esc(T('dbg.run')) + '</button></div>';
    try {
      var d = await ND.api('prediction_accuracy?lead=' + lead + '&range=' + range), s = d.stats || {}, u = d.unit || ND.sym(ND.unit), g = d.gate_summary || {}, m = d.model || {};
      var tile = function (l, v) { return '<div class="tile"><div class="l">' + esc(l) + '</div><div class="v">' + esc(v) + '</div></div>'; };
      var f = function (x) { return x == null ? '—' : Number(x).toFixed(2) + ' ' + u; };
      host.innerHTML = head + '<div class="note" style="margin:8px 0">' + esc(g.scored != null ? T('dbg.v4_beats', { passed: g.passed, scored: g.scored, days: g.window_days || 30, total: g.total }) : T('dbg.nothing_scored')) + ' ' + esc(T('dbg.main_prediction', { engine: d.primary_engine || 'v4' })) +
        ' ' + esc(T('dbg.model_line', { conf: m.confidence || T('dbg.na'), span: m.span_days != null ? T('dbg.span_days', { n: m.span_days }) : '', fitted: m.fitted_at ? T('dbg.fitted_at', { t: ND.fmtDateTime(m.fitted_at) }) : '' })) + '</div>' +
        '<div class="tiles">' + tile(T('dbg.points_scored'), s.n != null ? String(s.n) : '—') + (d.primary_engine !== 'v3' ? tile(T('dbg.v4_avg_miss'), f(s.v4_mae)) + tile(T('dbg.v3_avg_miss'), f(s.v3_mae)) : tile(T('dbg.v3_avg_miss'), f(s.v3_mae)) + tile(T('dbg.v4_avg_miss'), f(s.v4_mae))) + tile(T('dbg.no_change_miss'), f(s.flat_mae)) + (d.primary_engine !== 'v3' ? tile(T('dbg.v4_bias'), f(s.v4_bias)) + tile(T('dbg.v3_bias'), f(s.v3_bias)) : tile(T('dbg.v3_bias'), f(s.v3_bias)) + tile(T('dbg.v4_bias'), f(s.v4_bias))) + '</div>';
    } catch (e) { host.innerHTML = head + '<div class="empty" style="margin-top:8px">' + esc(T('dbg.backtest_unavailable', { msg: e.message })) + '</div>'; }
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
    // Issue #1491: same frame, never a new tab (a new tab has no Home Assistant login around it); Back returns here
    $('dbg-bt-link').addEventListener('click', function () {
      var z = ND.zone; if (z) this.href = 'prediction_accuracy.html?zone=' + encodeURIComponent(z);
      ND.rememberReturnTab('debug');
    });
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
