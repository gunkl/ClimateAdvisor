/* Issue #1230 R5/R6fe: the new dashboard's Status tab, assembled from the shared parts.

   What is happening now and what do I need to do?
     - "For you" / "CA next": the status payload's next_action / next_automation_action / next_automation_time
       (the operation timeline is authoritative for them since R3b; nothing here recomputes them);
     - the reading cards (every reading the classic Status grid had: per indoor sensor, outdoor sensor against the
       weather service, today, doors / windows with the fan, occupancy with the comfort score);
     - the temperature chart: the shared module chart_v2.js (the same code the classic Status tab runs);
     - "Next 24 hours": the operation timeline's steps (chart_v2.js renderPlanList), with the nat-vent cycling range;
     - "Tonight in the bedroom" on GET /api/climate_advisor/night_strategy (nd_night_card.js);
     - the briefing, collapsed, with Regenerate and Send.
   Banners, the health line and the Actions menu are in nd_core.js. Everything here is display only. */
(function () {
  'use strict';
  var T = window.CA_I18N.t;
  var loaded = false, card = null, pollCount = 0;

  function esc(s) { return ND.esc(s); }
  function sym() { return ND.sym(ND.unit); }

  // ---------- reading cards ----------
  function latestReading(cd, sid) {
    var arr = ((cd || {}).actual_indoor_sensors || {})[sid];
    return arr && arr.length ? arr[arr.length - 1] : null;
  }
  function serviceNow(cd) {
    if (!cd) return null;
    var log = cd.state_log || [];
    for (var i = log.length - 1; i >= 0; i--) {
      var v = log[i].pred_outdoor != null ? log[i].pred_outdoor : log[i].pred_outdoor_avg;
      if (v != null) return v;
    }
    var f = cd.forecast_outdoor || [];
    return f.length ? f[0].temp : null;
  }
  // ---------- plain words for the reading cards (Issue #1399) ----------
  // Every line on a card says what it is: no bare state tokens ("active", "idle", "inactive") and no signed gap shown
  // with a unit as if it were a reading. Exposed as ND.statusWords for the browser tests.
  function cap(s) { s = String(s == null ? '' : s); return s.charAt(0).toUpperCase() + s.slice(1); }
  // Two readings, worded as a difference: "The weather service says 54.0°F, 6.2° warmer than your outdoor sensor". Both values
  // are already in the display unit, so the gap needs no conversion (a gap never takes the −32 offset). Under half a
  // degree it "matches".
  function saysVs(who, theirs, yours, u, yourName) {
    var gap = Math.round((theirs - yours) * 10) / 10, mag = Math.abs(gap);
    return T('nds.says_vs', {
      who: who, theirs: theirs.toFixed(1), u: u,
      rest: mag < 0.5 ? T('nds.matches', { name: yourName }) : T(gap > 0 ? 'nds.warmer_than' : 'nds.cooler_than', { mag: mag.toFixed(1), name: yourName })
    });
  }
  // Tomorrow against today (the classifier's trend: a gap, never a reading).
  function trendWords(dir, mag) {
    var m = Math.round(Math.abs(mag || 0) * 10) / 10;
    if (dir === 'warming') return T('nds.trend_warming', { m: m.toFixed(1) });
    if (dir === 'cooling') return T('nds.trend_cooling', { m: m.toFixed(1) });
    return T('nds.trend_same');
  }
  // automation_status as a phrase: "following today's plan", "cooling with outside air (nat-vent)", or the status's
  // own phrase ("paused — door/window open", "grace period (manual) — 20 min ...").
  function automationPhrase(s) {
    s = String(s == null ? '' : s).trim();
    var m = /^active(?: \((away|vacation|guest)\))?(.*)$/.exec(s);
    if (m) return (m[1] ? T('nds.following_mode_plan', { mode: T('nds.mode_' + m[1]) }) : T('nds.following_todays_plan')) + m[2];
    if (s === 'disabled') return T('nds.paused_not_changing');
    if (!s || s === 'unknown') return T('nds.status_not_known');
    if (/^nat-vent/.test(s)) return T('nds.cooling_outside_air', { s: s });
    return s;
  }
  // A fan status as a sentence: "Whole-house fan is off", "HVAC fan is running (manual override)".
  function fanState() {
    return { inactive: T('nds.fan_is_off'), off: T('nds.fan_is_off'), active: T('nds.fan_is_running'), running: T('nds.fan_is_running'), disabled: T('nds.fan_not_managed') };
  }
  function fanSentence(name, s) {
    s = String(s == null ? '' : s).trim();
    if (s === 'active (unconfirmed)') return T('nds.fan_should_be_running', { name: name });
    if (/^nat-vent \(session active, fan idle\)/.test(s)) return T('nds.fan_resting', { name: name }) + s.replace(/^nat-vent \(session active, fan idle\)/, '');
    var m = /^(inactive|active|running|off|disabled)\b(.*)$/.exec(s);
    return m ? name + ' ' + fanState()[m[1]] + m[2] : name + ': ' + (s || T('nds.status_not_known_lc'));
  }
  // What the thermostat is doing, in one line: "Heating to 68°F · idle now", "Keeping between 66°F and 74°F", with
  // "Climate Advisor wants 70°F" when the thermostat disagrees with CA (#266, #402, #1194 d).
  function actionWords() {
    return { heating: T('nds.act_heating'), cooling: T('nds.act_cooling'), idle: T('nds.act_idle'), fan: T('nds.act_fan'), drying: T('nds.act_drying'), preheating: T('nds.act_preheating'), defrosting: T('nds.act_defrosting') };
  }
  function hvacSentence(d, u) {
    var mode = d.hvac_mode, sp = d.current_setpoint, want = '', set;
    if (mode === 'heat_cool' && (d.target_temp_low != null || d.target_temp_high != null)) {
      var lo = d.target_temp_low != null ? Math.round(d.target_temp_low) + u : '—';
      var hi = d.target_temp_high != null ? Math.round(d.target_temp_high) + u : '—';
      set = T('nds.keeping_between', { lo: lo, hi: hi });
      if (d.ca_target_heat != null && d.ca_target_cool != null && d.target_temp_low != null && d.target_temp_high != null &&
        (Math.abs(d.target_temp_low - d.ca_target_heat) > 1 || Math.abs(d.target_temp_high - d.ca_target_cool) > 1)) {
        want = Math.round(d.ca_target_heat) + u + '–' + Math.round(d.ca_target_cool) + u;
      }
    } else if (mode === 'off') {
      set = T('nds.thermostat_off');
    } else {
      var spText = sp != null ? sp + u : null;
      set = mode === 'heat' && spText ? T('nds.heating_to', { t: spText }) : mode === 'cool' && spText ? T('nds.cooling_to', { t: spText })
        : mode === 'fan_only' ? T('nds.fan_only') : mode === 'dry' ? T('nds.drying') : spText ? T('nds.set_to', { t: spText })
        : T('nds.thermostat_mode', { mode: mode || T('nds.not_known') });
      if (sp != null) {
        var ro = d.remote_offset;
        var ca = (ro && ro.expected_setpoint != null && ro.mode === mode) ? ro.expected_setpoint
          : mode === 'cool' ? d.ca_target_cool : mode === 'heat' ? d.ca_target_heat : null;
        if (ca != null && Math.abs(sp - ca) > 1) want = Math.round(ca) + u;
      }
    }
    var act = actionWords()[d.hvac_action] || (d.hvac_action && d.hvac_action !== 'off' ? d.hvac_action + ' ' + T('nds.now_suffix') : '');
    return esc(set) + (act ? ' · ' + esc(act) : '') + (want ? ' <span class="sub">(' + esc(T('nds.ca_wants', { t: want })) + ')</span>' : '');
  }
  function fanLines(d) {
    var out = '';
    if (d.whf_status != null) out += '<div class="n">' + esc(fanSentence(T('nds.whole_house_fan'), d.whf_status)) + '</div>';
    if (d.hvac_fan_status != null) out += '<div class="n">' + esc(fanSentence(T('nds.hvac_fan'), d.hvac_fan_status)) + '</div>';
    if (d.whf_status == null && d.hvac_fan_status == null) out += '<div class="n">' + esc(fanSentence(T('nds.fan'), d.fan_status || 'disabled')) + '</div>';
    if (d.fan_remote_timer_hours != null) {
      out += '<div class="n">' + esc(T('nds.fan_timer_remote', { h: d.fan_remote_timer_hours })) + (d.fan_remote_timer_ends ? esc(T('nds.timer_ends', { t: ND.fmtTime(d.fan_remote_timer_ends) })) : '') + '</div>';
    }
    if (d.fan_remote_speed != null) out += '<div class="n">' + esc(T('nds.remote_set_speed', { speed: d.fan_remote_speed })) + '</div>';
    return out;
  }
  ND.statusWords = { saysVs: saysVs, trendWords: trendWords, automationPhrase: automationPhrase, fanSentence: fanSentence, hvacSentence: hvacSentence };
  function tile(id, label, value, notes, title) {
    return '<div class="tile" id="' + id + '"' + (title ? ' title="' + esc(title) + '"' : '') + '><div class="l" title="' + label + '">' + label + '</div><div class="v">' + value + '</div>' + (notes || '') + '</div>';
  }

  function renderCards() {
    var d = ND.status, grid = ND.$('status-grid');
    if (!d || !grid) return;
    var cd = window.CAChartV2 ? CAChartV2.data() : null;
    var u = sym();
    var indoor = d.indoor_temp != null ? d.indoor_temp.toFixed(1) + u : '—';
    var hvacNote = '<div class="n" id="status-hvac-line">' + hvacSentence(d, u) + '</div>' +
      (d.remote_offset && d.remote_offset.text ? '<div class="n remote-offset-line">' + esc(d.remote_offset.text) + '</div>' : '');
    // What is happening now, and why: the Status sentence rides on the hallway (thermostat) card. The header health
    // line only says whether automation is on; this says what it is doing (nat-vent, a grace period, a pause).
    var statusLine = '<div class="n" id="status-automation-line">' + esc(T('nds.ca_colon')) + ' ' + esc(automationPhrase(d.automation_status)) + '</div>';
    var cards = [];
    var metas = ((cd || {}).indoor_sensors || []).filter(function (m) { return latestReading(cd, m.id); });
    if (!metas.length) {
      cards.push(tile('status-card-hallway', T('nds.indoor_hvac'), indoor, hvacNote + statusLine +
        (d.sleep_indoor_sensor_active ? '<div class="n" id="status-sleep-sensor">' + esc(T('nds.reads_bedroom_sensor')) + '</div>' : '')));
    } else {
      var holder = metas.filter(function (m) { return m.role === 'thermostat'; })[0] || metas.filter(function (m) { return m.primary; })[0] || metas[0];
      metas.forEach(function (m, i) {
        var r = latestReading(cd, m.id), isBed = m.role === 'bedroom sensor';
        var reads = isBed ? !!d.sleep_indoor_sensor_active : (m.primary && !d.sleep_indoor_sensor_active);
        var id = m === holder ? 'status-card-hallway' : isBed ? 'status-card-bedroom' : 'status-card-sensor-' + i;
        var note = (m === holder ? hvacNote + statusLine : '') +
          (reads ? '<div class="n"' + (isBed ? ' id="status-sleep-sensor"' : '') + '>' + esc(T('nds.reads_this_sensor')) + '</div>'
            : isBed ? '<div class="n" id="status-sleep-sensor">' + esc(T('nds.reads_in_sleep_window')) + '</div>' : '');
        cards.push(tile(id, esc(m.name) + ' · ' + esc(m.role === 'thermostat' ? T('nds.thermostat_lc') : m.role), r.temp.toFixed(1) + u, note, T('nds.logged') + ' ' + ND.fmtTime(r.time)));
      });
    }
    // Outdoor sensor against the weather service (and the corrected forecast once v4 is shown).
    var svc = serviceNow(cd), v4 = (cd || {}).v4 || {};
    var corr = v4.ready && v4.corrected_outdoor && v4.corrected_outdoor.future && v4.corrected_outdoor.future.length ? v4.corrected_outdoor.future[0].temp : null;
    var outNote = '';
    if (svc != null && d.outdoor_temp != null) {
      outNote += '<div class="n" id="status-outdoor-vs-service" title="' + esc(T('nds.weather_vs_sensor_title')) + '">' + esc(saysVs(T('nds.weather_service'), svc, d.outdoor_temp, u, T('nds.your_outdoor_sensor'))) + '</div>';
    }
    if (corr != null) outNote += '<div class="n" title="' + esc(T('nds.corrected_forecast_title')) + '">' + esc(T('nds.corrected_forecast', { t: corr.toFixed(1) + u })) + '</div>';
    cards.push(tile('status-card-outdoor', T('nds.outdoor_sensor'), d.outdoor_temp != null ? d.outdoor_temp.toFixed(1) + u : '—', outNote));
    // Today
    cards.push(tile('status-card-today', T('nds.today'), esc(cap(T('nds.daytype_' + (d.day_type || 'unknown')))),
      '<div class="n">' + trendWords(d.trend_direction, d.trend_magnitude) + '</div>' +
      (d.hvac_runtime_today != null ? '<div class="n">' + esc(T('nds.system_ran', { min: Math.round(d.hvac_runtime_today) })) + '</div>' : '')));
    // Doors, windows and the fan
    var hasContacts = d.contact_sensors && d.contact_sensors.length > 0;
    cards.push(tile('status-card-doors', T('nds.doors_windows_fan'),
      hasContacts ? esc(cap(d.contact_status || 'unknown')) : T('nds.no_sensors'),
      (hasContacts ? d.contact_sensors.filter(function (c) { return c.unreadable; }).map(function (c) {
        return '<div class="n">' + esc(T('nds.sensor_not_responding', { name: c.friendly_name, reading: c.last_reading || T('core.never'), treated_as: c.treated_as })) + '</div>';
      }).join('') : '') + fanLines(d)));
    // Occupancy with the comfort score
    cards.push(tile('status-card-occupancy', T('nds.occupancy_comfort'), esc(cap(T('nds.mode_' + (d.occupancy_mode || 'home')))),
      '<div class="n">' + esc(T('nds.comfort_score', { pct: Math.round((d.compliance_score || 0) * 100) })) + '</div>'));
    grid.innerHTML = cards.join('');
  }

  function renderNext() {
    var d = ND.status;
    if (!d) return;
    ND.$('status-for-you').textContent = d.next_action || T('nds.none');
    ND.$('status-ca-next').textContent = d.next_automation_action || T('nds.none');
    ND.$('status-ca-next-time').textContent = d.next_automation_time || '—';
  }

  // ---------- plan list ----------
  function natVentLine() { // the nat-vent cycling range belongs to the plan list
    var d = ND.status;
    if (!d || !d.nat_vent_active || d.nat_vent_off_threshold == null || d.nat_vent_on_threshold == null) return '';
    var u = sym();
    return esc((d.nat_vent_ac_assist ? T('nds.ac_assist') : T('nds.savings_mode')) + ' · ' + T('nds.cycling', { lo: Math.round(d.nat_vent_off_threshold) + u, hi: Math.round(d.nat_vent_on_threshold) + u }));
  }

  // ---------- night card ----------
  function noNightMsg() { return T('nds.no_night'); }
  function noNight(e) {
    var r = e && e.body && e.body.reason;
    return r === 'no_night_in_view' ? T('nds.no_night_day') : r === 'v4_off' ? T('nds.v4_off') : noNightMsg();
  }
  function setNight(mode, data, msg) { // mode: live | sample | empty | error
    var host = ND.$('night-card');
    ND.$('night-sample-badge').hidden = mode !== 'sample';
    if (mode === 'empty' || mode === 'error') {
      card = null;
      host.innerHTML = '<div class="' + (mode === 'error' ? 'err' : 'empty') + '" id="night-empty">' + esc(msg) + '</div>';
      return;
    }
    if (card) { card.update(data); return; }
    host.innerHTML = '';
    card = window.NDNightCard.mount(host, data, { unit: ND.unit });
  }
  function loadNight() {
    return ND.api('night_strategy').then(function (p) {
      var data = p && p.options ? window.NDNightCard.fromStrategy(p) : null;
      if (!data || !data.nights.length) setNight('empty', null, noNightMsg());
      else setNight('live', data);
    }).catch(function (e) {
      if (e.status === 503 || e.message === 'HTTP 503') { setNight('empty', null, noNight(e)); return; }
      if (e.status === 404 || e.message === 'HTTP 404') { // an install without the endpoint: the approved mock, labelled as a sample
        return fetch('nd_night_sample.json').then(function (r) { return r.json(); }).then(function (d) { setNight('sample', d); })
          .catch(function () { setNight('error', null, T('nds.night_load_failed')); });
      }
      setNight('error', null, T('nds.night_load_failed_msg', { msg: e.message }));
    });
  }

  // ---------- briefing ----------
  function loadBriefing() {
    return ND.api('briefing').then(function (b) {
      ND.$('status-briefing-text').textContent = (b && b.briefing) || T('nds.no_briefing_yet');
    }).catch(function (e) {
      ND.$('status-briefing-text').textContent = T('nds.briefing_load_failed', { msg: e.message });
    });
  }
  function initBriefing() {
    ND.$('status-briefing-regenerate').addEventListener('click', function (e) {
      // the classic Regenerate: refresh the plan shown here, no push or email
      ND.runAction('send_briefing', { notify: false }, e.currentTarget);
    });
    ND.$('status-briefing-send').addEventListener('click', function (e) {
      ND.runAction('send_briefing', undefined, e.currentTarget);
    });
  }

  // ---------- loading ----------
  function reloadPlan() { loadBriefing(); CAChartV2.loadTimeline(); loadNight(); CAChartV2.load(); }
  function loadAll() { loaded = true; reloadPlan(); }

  ND.on('init', function () {
    CAChartV2.init({
      zone: function () { return ND.zone; },
      api: function (path) { return ND.api(path); },
      onData: function () { renderCards(); }
    });
    CAChartV2.mountPlan(ND.$('status-plan-list'), { extra: natVentLine });
    initBriefing();
  });
  ND.on('status', function (s) {
    CAChartV2.setStatus(s);
    renderNext(); renderCards();
    CAChartV2.renderPlanList(ND.$('status-plan-list'), { extra: natVentLine });
  });
  ND.on('zone', function () { if (loaded) loadAll(); });
  ND.on('refresh', function () { if (loaded) loadAll(); });
  ND.on('state-changed', function () { if (loaded) setTimeout(loadAll, 1200); });
  // re-plan and briefing actions change the plan, the briefing and the chart
  ND.on('action', function (a) { if (loaded && (a === 'force_reclassify' || a === 'send_briefing')) setTimeout(reloadPlan, 1200); });
  ND.on('poll', function () { // status polls every minute: the plan every minute, the chart, night plan and briefing every fifth
    pollCount++;
    if (!loaded) return;
    CAChartV2.loadTimeline();
    if (pollCount % 5 === 0) { CAChartV2.load(); loadNight(); loadBriefing(); }
  });

  function show() {
    if (!loaded) loadAll();
    else { CAChartV2.redraw(); CAChartV2.loadTimeline(); }
  }
  ND.register('status', { show: show });
})();
