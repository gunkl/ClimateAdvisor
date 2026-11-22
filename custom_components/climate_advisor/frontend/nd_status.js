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
  // with a unit as if it were a reading. Exposed as ND.statusWords for the browser tests. Issue #1341: every sentence is
  // a catalog message (dashboard.status.*); the helpers return plain text and the card sinks isolate the numbers.
  var S = 'dashboard.status.';
  function t(key, params) { return ND.t(S + key, params); }
  function num(v, max) { return CAI18N.fmt.number(Number(v), { maximumFractionDigits: max == null ? 1 : max, useGrouping: false }); }
  function cap(s) { s = String(s == null ? '' : s); return s.charAt(0).toUpperCase() + s.slice(1); }
  // A backend code word (day type, occupancy, treated-as) as a catalog word; an unlisted one is shown as sent.
  function word(group, code, fallbackCap) {
    if (code === 'unknown') return ND.t('common.unknown');
    var k = S + group + '_' + String(code == null ? '' : code).toLowerCase().replace(/[^a-z0-9]+/g, '_');
    if (code != null && code !== '' && CAI18N.has(k)) return ND.t(k);
    return fallbackCap ? cap(code) : String(code == null ? '' : code);
  }
  // Two readings, worded as a difference: "The weather service says 54.0°F, 6.2° warmer than your outdoor sensor". Both values
  // are already in the display unit, so the gap needs no conversion (a gap never takes the −32 offset). Under half a
  // degree it "matches". who / yourName are phrases (catalog messages at the call site).
  function saysVs(who, theirs, yours, u, yourName) {
    var gap = Math.round((theirs - yours) * 10) / 10, mag = Math.abs(gap);
    return t(mag < 0.5 ? 'says_matches' : gap > 0 ? 'says_warmer' : 'says_cooler',
      { who: who, theirs: ND.num(theirs, 1) + u, name: yourName, gap: ND.num(mag, 1) });
  }
  // Tomorrow against today (the classifier's trend: a gap, never a reading). The sideways arrow mirrors in RTL.
  function trendWords(dir, mag) {
    var m = ND.num(Math.round(Math.abs(mag || 0) * 10) / 10, 1);
    if (dir === 'warming') return '↑ ' + t('trend_warmer', { n: m });
    if (dir === 'cooling') return '↓ ' + t('trend_cooler', { n: m });
    return (ND.rtl() ? '←' : '→') + ' ' + t('trend_same');
  }
  // automation_status as a phrase: "following today's plan", "cooling with outside air (nat-vent)", or the status's
  // own phrase ("paused — door/window open", "grace period (manual) — 20 min ...").
  // Branches on the payload's stable `status_code` (never on the English text). The message itself (`automation_status_msg`)
  // is the status in the viewer's language; the prose field (backend language) is all there is when the backend sent no
  // message (a plain-text status): it is shown as sent.
  function wireText(w) { return w == null ? '' : CAI18N.tw(w); }
  function automationPhrase(d) {
    d = d || {};
    var code = d.status_code, shown = ND.text(d, 'automation_status');
    var act = /^active(?:_(away|vacation|guest))?$/.exec(code || '');
    if (act) { // "active" plus the fan requests that ride on it (with_fan_requests) is still "following the plan"
      var w = d.automation_status_msg, rest = w && w.key === 'status.auto.with_fan_requests' && w.params ? wireText(w.params.requests) : '';
      return t('phrase_following', { plan: act[1] || 'today', rest: rest });
    }
    if (code === 'disabled') return t('phrase_disabled');
    if (code === 'nat_vent' || code === 'nat_vent_soft_start') return t('phrase_natvent', { status: shown });
    if (!shown || shown === 'unknown') return t('phrase_unknown');
    return shown;
  }
  // A fan status as a sentence: "Whole-house fan is off", "HVAC fan is running (manual override)". Branches on the
  // payload's `<field>_code`; a status with rate-limit / blind-indoor notes (or a code this page does not know) is the
  // backend's own message after the name.
  var FAN_SENTENCE = {
    disabled: 'fan_unmanaged', inactive: 'fan_off', active: 'fan_running', active_unconfirmed: 'fan_unconfirmed',
    running_manual_override: 'fan_running_manual', off_manual_override: 'fan_off_manual', running_untracked: 'fan_running_untracked',
    running_thermostat: 'fan_running_thermostat', nat_vent_idle: 'fan_resting'
  };
  function fanSentence(name, d, field) {
    d = d || {};
    var code = d[field + '_code'], w = d[field + '_msg'], key = FAN_SENTENCE[code];
    if (key && !(w && w.key === 'status.fan_status.compound')) return t(key, { name: name, rest: '' });
    return t('fan_line', { name: name, status: ND.text(d, field) || t('fan_unknown') });
  }
  // What the thermostat is doing, in one line: "Heating to 68°F · idle now", "Keeping between 66°F and 74°F", with
  // "Climate Advisor wants 70°F" when the thermostat disagrees with CA (#266, #402, #1194 d). Returns HTML.
  var ACTION_KEYS = { heating: 'act_heating', cooling: 'act_cooling', idle: 'act_idle', fan: 'act_fan', drying: 'act_drying', preheating: 'act_preheating', defrosting: 'act_defrosting' };
  function hvacSentence(d, u, isolate) { // isolate: wrap the numbers in <bdi> (the card sink); the plain form is what tests read
    var fmt = isolate ? ND.iso : esc;
    var mode = d.hvac_mode, sp = d.current_setpoint, want = '', set;
    function T(v) { return num(v, 1) + u; }
    function R(v) { return num(Math.round(v), 0) + u; }
    if (mode === 'heat_cool' && (d.target_temp_low != null || d.target_temp_high != null)) {
      var lo = d.target_temp_low != null ? R(d.target_temp_low) : '—';
      var hi = d.target_temp_high != null ? R(d.target_temp_high) : '—';
      set = t('hvac_between', { lo: lo, hi: hi });
      if (d.ca_target_heat != null && d.ca_target_cool != null && d.target_temp_low != null && d.target_temp_high != null &&
        (Math.abs(d.target_temp_low - d.ca_target_heat) > 1 || Math.abs(d.target_temp_high - d.ca_target_cool) > 1)) {
        want = R(d.ca_target_heat) + '–' + R(d.ca_target_cool);
      }
    } else if (mode === 'off') {
      set = t('hvac_off');
    } else {
      var spText = sp != null ? T(sp) : null;
      set = mode === 'heat' && spText ? t('hvac_heating_to', { temp: spText }) : mode === 'cool' && spText ? t('hvac_cooling_to', { temp: spText })
        : mode === 'fan_only' ? t('hvac_fan_only') : mode === 'dry' ? t('hvac_drying') : spText ? t('hvac_set_to', { temp: spText })
        : mode ? t('hvac_mode', { mode: CAI18N.has('common.mode.' + mode) ? ND.t('common.mode.' + mode) : String(mode) }) : t('hvac_mode_unknown');
      if (sp != null) {
        var ro = d.remote_offset;
        var ca = (ro && ro.expected_setpoint != null && ro.mode === mode) ? ro.expected_setpoint
          : mode === 'cool' ? d.ca_target_cool : mode === 'heat' ? d.ca_target_heat : null;
        if (ca != null && Math.abs(sp - ca) > 1) want = R(ca);
      }
    }
    var act = ACTION_KEYS[d.hvac_action] ? t(ACTION_KEYS[d.hvac_action]) : (d.hvac_action && d.hvac_action !== 'off' ? t('act_other', { action: d.hvac_action }) : '');
    return fmt(set) + (act ? ' · ' + fmt(act) : '') + (want ? ' <span class="sub">' + fmt(t('hvac_wants', { want: want })) + '</span>' : '');
  }
  function fanLines(d) {
    var out = '';
    if (d.whf_status != null) out += '<div class="n">' + ND.iso(fanSentence(t('name_whf'), d, 'whf_status')) + '</div>';
    if (d.hvac_fan_status != null) out += '<div class="n">' + ND.iso(fanSentence(t('name_hvac_fan'), d, 'hvac_fan_status')) + '</div>';
    if (d.whf_status == null && d.hvac_fan_status == null) out += '<div class="n">' + ND.iso(fanSentence(t('name_fan'), d, 'fan_status')) + '</div>';
    if (d.fan_remote_timer_hours != null) {
      out += '<div class="n">' + ND.iso(d.fan_remote_timer_ends
        ? t('fan_timer_ends', { hours: num(d.fan_remote_timer_hours, 1), time: ND.fmtTime(d.fan_remote_timer_ends) })
        : t('fan_timer', { hours: num(d.fan_remote_timer_hours, 1) })) + '</div>';
    }
    if (d.fan_remote_speed != null) out += '<div class="n">' + ND.iso(t('fan_remote_speed', { speed: word('fan_speed', d.fan_remote_speed) })) + '</div>';
    return out;
  }
  ND.statusWords = { saysVs: saysVs, trendWords: trendWords, automationPhrase: automationPhrase, fanSentence: fanSentence, hvacSentence: hvacSentence };
  // label is plain text (escaped here); value and notes are trusted HTML built by the callers.
  function tile(id, label, value, notes, title) {
    return '<div class="tile" id="' + id + '"' + (title ? ' title="' + esc(title) + '"' : '') + '><div class="l" title="' + esc(label) + '">' + esc(label) + '</div><div class="v">' + value + '</div>' + (notes || '') + '</div>';
  }
  // A sensor's role: the backend's `role_msg` (status.sensor_role.*) in the viewer's language; `role_code` is thermostat |
  // finer_sensor | bedroom_sensor | room_sensor | ca_reading.
  function roleText(m) { return ND.text(m, 'role'); }

  function renderCards() {
    var d = ND.status, grid = ND.$('status-grid');
    if (!d || !grid) return;
    var cd = window.CAChartV2 ? CAChartV2.data() : null;
    var u = sym();
    function temp1(v) { return v != null ? ND.bdi(ND.num(v, 1) + u) : '—'; }
    var indoor = temp1(d.indoor_temp);
    var hvacNote = '<div class="n" id="status-hvac-line">' + hvacSentence(d, u, true) + '</div>' +
      (d.remote_offset && d.remote_offset.text ? '<div class="n remote-offset-line">' + esc(ND.text(d.remote_offset, 'text')) + '</div>' : '');
    // What is happening now, and why: the Status sentence rides on the hallway (thermostat) card. The header health
    // line only says whether automation is on; this says what it is doing (nat-vent, a grace period, a pause).
    var statusLine = '<div class="n" id="status-automation-line">' + ND.iso(t('automation_line', { phrase: automationPhrase(d) })) + '</div>';
    // Issue #1676: the thermostat reads falsely high after a heating stop; say so, and which sensor CA reads meanwhile.
    var recal = d.thermostat_recalibration || null;
    if (recal) {
      statusLine += '<div class="n" id="status-thermostat-recal">' + esc(recal.name
        ? t('thermostat_recal_reading', { name: recal.name }) : t('thermostat_recal_no_alt')) + '</div>';
    }
    var cards = [];
    var metas = ((cd || {}).indoor_sensors || []).filter(function (m) { return latestReading(cd, m.id); });
    if (!metas.length) {
      cards.push(tile('status-card-hallway', t('card_indoor_hvac'), indoor, hvacNote + statusLine +
        (d.sleep_indoor_sensor_active ? '<div class="n" id="status-sleep-sensor">' + esc(t('reads_bedroom_sleep')) + '</div>' : '')));
    } else {
      var holder = metas.filter(function (m) { return m.role_code === 'thermostat'; })[0] || metas.filter(function (m) { return m.primary; })[0] || metas[0];
      metas.forEach(function (m, i) {
        var r = latestReading(cd, m.id), isBed = m.role_code === 'bedroom_sensor';
        var reads = recal && recal.entity ? m.id === recal.entity
          : isBed ? !!d.sleep_indoor_sensor_active : (m.primary && !d.sleep_indoor_sensor_active);
        var id = m === holder ? 'status-card-hallway' : isBed ? 'status-card-bedroom' : 'status-card-sensor-' + i;
        var note = (m === holder ? hvacNote + statusLine : '') +
          (reads ? '<div class="n"' + (isBed ? ' id="status-sleep-sensor"' : '') + '>' + esc(t('reads_this_sensor')) + '</div>'
            : isBed ? '<div class="n" id="status-sleep-sensor">' + esc(t('reads_in_sleep')) + '</div>' : '');
        cards.push(tile(id, t('sensor_head', { name: m.name, role: roleText(m) }), temp1(r.temp), note, t('logged_at', { time: ND.fmtTime(r.time) })));
      });
    }
    // Outdoor sensor against the weather service (and the corrected forecast once v4 is shown).
    var svc = serviceNow(cd), v4 = (cd || {}).v4 || {};
    var corr = v4.ready && v4.corrected_outdoor && v4.corrected_outdoor.future && v4.corrected_outdoor.future.length ? v4.corrected_outdoor.future[0].temp : null;
    var outNote = '';
    if (svc != null && d.outdoor_temp != null) {
      outNote += '<div class="n" id="status-outdoor-vs-service" title="' + esc(t('outdoor_vs_service_title')) + '">' + ND.iso(saysVs(t('the_weather_service'), svc, d.outdoor_temp, u, t('your_outdoor_sensor'))) + '</div>';
    }
    if (corr != null) outNote += '<div class="n" title="' + esc(t('corrected_title')) + '">' + ND.iso(t('corrected_forecast', { temp: ND.num(corr, 1) + u })) + '</div>';
    cards.push(tile('status-card-outdoor', t('card_outdoor'), temp1(d.outdoor_temp), outNote));
    // Today
    cards.push(tile('status-card-today', t('card_today'), esc(d.day_type ? word('day_type', d.day_type, true) : ND.t('common.unknown')),
      '<div class="n">' + ND.iso(trendWords(d.trend_direction, d.trend_magnitude)) + '</div>' +
      (d.hvac_runtime_today != null ? '<div class="n">' + ND.iso(t('ran_today', { n: Math.round(d.hvac_runtime_today) })) + '</div>' : '')));
    // Doors, windows and the fan
    var hasContacts = d.contact_sensors && d.contact_sensors.length > 0;
    cards.push(tile('status-card-doors', t('card_doors_fan'),
      hasContacts ? esc(cap(ND.text(d, 'contact_status') || ND.t('common.unknown'))) : esc(t('no_sensors')),
      (hasContacts ? d.contact_sensors.filter(function (c) { return c.unreadable; }).map(function (c) {
        return '<div class="n">' + ND.th(S + 'contact_not_responding', { name: c.friendly_name, last: c.last_reading ? word('treated', c.last_reading, false) : ND.t('dashboard.core.never'), treated: word('treated', c.treated_as, false) }) + '</div>';
      }).join('') : '') + fanLines(d)));
    // Occupancy with the comfort score
    cards.push(tile('status-card-occupancy', t('card_occupancy'), esc(word('occupancy', d.occupancy_mode || 'home', true)),
      '<div class="n">' + ND.iso(t('comfort_score', { score: Math.round((d.compliance_score || 0) * 100) / 100 })) + '</div>'));
    grid.innerHTML = cards.join('');
  }

  function renderNext() {
    var d = ND.status;
    if (!d) return;
    ND.$('status-for-you').textContent = ND.text(d, 'next_action') || t('none');
    ND.$('status-ca-next').textContent = ND.text(d, 'next_automation_action') || t('none');
    ND.$('status-ca-next-time').textContent = ND.text(d, 'next_automation_time') || '—';
  }

  // ---------- plan list ----------
  function natVentLine() { // the nat-vent cycling range belongs to the plan list
    var d = ND.status;
    if (!d || !d.nat_vent_active || d.nat_vent_off_threshold == null || d.nat_vent_on_threshold == null) return '';
    var u = sym();
    return ND.iso(t(d.nat_vent_ac_assist ? 'natvent_assist' : 'natvent_savings',
      { off: num(Math.round(d.nat_vent_off_threshold), 0) + u, on: num(Math.round(d.nat_vent_on_threshold), 0) + u }));
  }

  // ---------- night card ----------
  // The 503 body's `reason` is a code ('no_night_in_view', 'v4_off'); anything else is the plain "no plan yet".
  function noNight(e) {
    var r = e && e.body && e.body.reason;
    return t(r === 'no_night_in_view' ? 'night_none_day' : r === 'v4_off' ? 'night_v4_off' : 'night_none');
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
      if (!data || !data.nights.length) setNight('empty', null, t('night_none'));
      else setNight('live', data);
    }).catch(function (e) {
      if (e.status === 503 || e.message === 'HTTP 503') { setNight('empty', null, noNight(e)); return; }
      if (e.status === 404 || e.message === 'HTTP 404') { // an install without the endpoint: the approved mock, labelled as a sample
        return fetch('nd_night_sample.json').then(function (r) { return r.json(); }).then(function (d) { setNight('sample', d); })
          .catch(function () { setNight('error', null, t('night_load_failed')); });
      }
      setNight('error', null, t('night_load_failed_why', { error: e.message }));
    });
  }

  // ---------- briefing ----------
  function loadBriefing() {
    return ND.api('briefing').then(function (b) {
      ND.$('status-briefing-text').textContent = (b && ND.text(b, 'briefing')) || t('briefing_none');
    }).catch(function (e) {
      ND.$('status-briefing-text').textContent = t('briefing_failed', { error: e.message });
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
