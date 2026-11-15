/* Issue #1230 R5: Settings tab. Read-only, exactly like classic: it reads /config (and the new read-only
   /settings_extra for changed-from-default, TOU schedules and GitHub status) and sends the owner to Home
   Assistant's own options flow to change anything. No write path exists here. */
(function () {
  'use strict';
  var $ = ND.$, esc = ND.esc, C = null, X = null, AI = null, loaded = false, cfgErr = null;
  var DESC_KEY = 'climate_advisor_nd_show_desc';

  // Issue #1341: catalog text (settings.*). T() renders through CAI18N; every result is escaped (esc) or set with
  // textContent. iso() isolates a dynamic Latin name (entity id, repo, model) inside RTL text.
  function T(key, params) { return CAI18N.t('settings.' + key, params); }
  function iso(x) { return CAI18N.dir === 'rtl' ? '\u2068' + x + '\u2069' : String(x); }
  // The backend's `<field>_msg` wire sibling when present (rendered in the viewer's language), else its English field.
  function W(wire, plain) { var r = wire ? CAI18N.tw(wire) : ''; return r && (!wire.key || r !== wire.key) ? r : plain; }
  function numText(v) { return CAI18N.fmt.number(Number(v), { maximumFractionDigits: 6, useGrouping: false }); }

  var G = [
    ['comfort', ['comfort_heat', 'comfort_cool', 'sleep_heat', 'sleep_cool', 'setback_heat', 'setback_cool', 'wake_time', 'sleep_time']],
    ['equipment', ['climate_entity', 'weather_entity', 'temp_unit', 'fan_mode', 'fan_entity', 'fan_remote_entity', 'fan_state_entity', 'fan_state_feedback', 'hvac_fan_restrict_mode', 'nat_vent_soft_start_enabled', 'fan_min_runtime_per_hour']],
    ['sensors', ['indoor_temp_source', 'outdoor_temp_source', 'thermostat_companion_sensor', 'sleep_indoor_temp_entity', 'indoor_humidity_entity', 'sleep_indoor_humidity_entity', 'outdoor_humidity_entity', 'door_window_sensors', 'sensor_polarity_inverted']],
    ['occupancy', ['home_toggle_entity', 'home_toggle_invert', 'vacation_toggle_entity', 'vacation_toggle_invert', 'vacation_heat', 'vacation_cool', 'guest_toggle_entity', 'guest_toggle_invert']],
    ['timing', ['sensor_debounce_seconds', 'welcome_home_debounce_seconds', 'override_confirm_seconds', 'manual_grace_seconds', 'automation_grace_seconds', 'comfort_mode_switch_min_interval_s', 'comfort_family_recency_window_min']],
    ['notify', ['briefing_time', 'briefing_notifications_enabled', 'notify_service', 'push_briefing', 'email_briefing', 'push_door_window_pause', 'email_door_window_pause', 'push_occupancy_home', 'email_occupancy_home', 'push_entity_health', 'email_entity_health', 'manual_grace_notify', 'automation_grace_notify', 'email_grace_expired', 'email_grace_repause']],
    ['learning', ['primary_engine', 'shadow_engine', 'learning_enabled', 'weather_bias_enabled', 'adaptive_setback_enabled', 'max_setback_depth_f', 'adaptive_preheat_enabled', 'default_preheat_minutes', 'min_preheat_minutes', 'max_preheat_minutes', 'preheat_safety_margin', 'default_tou_lead_minutes']],
    ['daytypes', ['threshold_hot', 'threshold_warm', 'threshold_mild', 'threshold_cool', 'comfort_deadband_hot_f', 'comfort_deadband_warm_f', 'comfort_deadband_mild_f', 'comfort_deadband_cool_f', 'comfort_deadband_cold_f', 'aggressive_savings', 'energy_rate', 'heat_power_kw', 'cool_power_kw']],
    ['ai', ['ai_enabled', 'ai_api_key', 'ai_model', 'ai_reasoning_effort', 'ai_max_tokens', 'ai_temperature', 'ai_monthly_budget', 'ai_auto_requests_per_day', 'ai_manual_requests_per_day', 'ai_investigator_enabled', 'ai_investigator_requests_per_day']]
  ];

  // A number with its explicit `unit` (the API's field: the temperature symbol, or "min" / "s" for the time settings;
  // the unit is never parsed out of a label).
  function withUnit(v, unit) {
    var mins = function (m) { return m >= 120 && m % 60 === 0 ? T('value.hours', { n: m / 60 }) : T('value.minutes', { n: m }); };
    if (unit === 'min') return mins(v);
    if (unit === 's') return v >= 120 ? mins(v / 60) : T('value.seconds', { n: v });
    if (unit === 'fahrenheit' || unit === '°F') return numText(v) + '°F';
    if (unit === 'celsius' || unit === '°C') return numText(v) + '°C';
    return numText(v) + (unit ? ' ' + unit : '');
  }
  // An enum-like setting (engine, fan mode, unit, ...): the stored code as its catalog word (settings.enum.<key>.<code>);
  // anything else (an entity id) is shown as stored.
  function enumText(key, v) {
    var k = 'enum.' + key + '.' + v;
    return typeof v === 'string' && CAI18N.has('settings.' + k) ? T(k) : null;
  }
  function fmt(s) {
    var v = s.value;
    if (s.value_msg) { var vm = CAI18N.tw(s.value_msg); if (vm && vm !== s.value_msg.key) return vm; }
    var en = enumText(s.key, v); if (en != null) return en;
    if (v === true) return T('value.on'); if (v === false) return T('value.off'); if (v == null || v === '') return T('value.not_set');
    if (Array.isArray(v)) return v.length ? v.join(', ') : T('value.none');
    if (typeof v === 'object') return JSON.stringify(v);
    if (typeof v === 'number') return withUnit(v, s.unit);
    return String(v) + (s.unit ? (s.unit === 'fahrenheit' || s.unit === '°F' ? '°F' : ' ' + s.unit) : '');
  }
  function labelOf(s) { return W(s.label_msg, s.label || s.key); }
  function descOf(s) { return W(s.description_msg, s.description || ''); }
  function defaultText(key, s) {
    var c = X && X.changed && X.changed[key]; if (!c) return '';
    var d = c.default, txt;
    if (d === true) txt = T('value.on'); else if (d === false) txt = T('value.off');
    else if (typeof d === 'number') txt = withUnit(d, c.unit || s.unit);
    else txt = (enumText(key, d) || String(d)) + (c.unit ? ' ' + c.unit : '');
    return T('default_value', { value: txt });
  }

  function render() {
    var host = $('set-groups');
    if (!C) { host.innerHTML = '<div class="err">' + esc(T('load_failed', { error: cfgErr || T('no_data') })) + '</div>'; $('set-count').textContent = ''; return; }
    var q = ($('set-search').value || '').toLocaleLowerCase(CAI18N.lang), showDesc = $('set-desc').checked, onlyChanged = $('set-changed').checked;
    var by = {}, used = {}, groups = [], total = 0;
    C.forEach(function (s) { by[s.key] = s; });
    function add(id, keys) {
      var rows = keys.filter(function (k) { return by[k]; }).map(function (k) { used[k] = 1; return by[k]; }).filter(function (s) {
        if (onlyChanged && !(X && X.changed && X.changed[s.key])) return false;
        return !q || (labelOf(s) + ' ' + fmt(s) + ' ' + descOf(s)).toLocaleLowerCase(CAI18N.lang).indexOf(q) >= 0;
      });
      if (rows.length) { groups.push({ id: id, title: T('group.' + id + '.title'), blurb: T('group.' + id + '.blurb'), rows: rows }); total += rows.length; }
    }
    G.forEach(function (g) { add(g[0], g[1]); });
    add('other', Object.keys(by).filter(function (k) { return !used[k]; }));
    host.innerHTML = groups.map(function (g) {
      return '<section class="card" id="settings-' + g.id + '" tabindex="-1" style="padding:10px 12px;gap:4px"><h2 style="font-size:15px">' + esc(g.title) + '</h2><div class="sub">' + esc(g.blurb) + '</div>' +
        g.rows.map(function (s) {
          var def = defaultText(s.key, s);
          return '<div style="border-top:1px solid var(--rule);padding:6px 0"><div style="display:flex;justify-content:space-between;gap:12px;font-size:13px"><span>' + esc(labelOf(s)) +
            (def ? ' <span class="badge changed">' + esc(T('badge_changed')) + '</span>' : '') + '</span><span class="num" style="font-weight:600;text-align:end;word-break:break-word"><bdi>' + esc(fmt(s)) + '</bdi></span></div>' +
            (def ? '<div class="sub" style="text-align:end">' + esc(def) + '</div>' : '') +
            (showDesc && descOf(s) ? '<div class="sub" style="margin-top:2px;line-height:1.4">' + esc(descOf(s)) + '</div>' : '') + '</div>';
        }).join('') + '</section>';
    }).join('') || '<div class="empty">' + esc(T('no_match')) + '</div>';
    $('set-jump').innerHTML = groups.map(function (g) { return '<a class="chip-link" href="#settings/' + g.id + '">' + esc(g.title) + '<span class="n">' + g.rows.length + '</span></a>'; }).join('');
    var nChanged = X && X.changed ? Object.keys(X.changed).length : null;
    $('set-count').textContent = nChanged != null ? T('count_changed', { shown: total, total: C.length, changed: nChanged }) : T('count', { shown: total, total: C.length });
    $('set-changed').disabled = !X;
    renderTou(); renderIntegrations();
  }

  // Weekday names from Intl (2024-01-01 is a Monday); the schedule's own day order is kept. The first weekday of the
  // week matters only to an editor; this tab is read-only, so there is no week layout to order.
  var DAY_CODES = ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun'];
  function dayName(code) {
    var i = DAY_CODES.indexOf(code);
    if (code === 'all') return T('tou.every_day');
    if (i < 0) return code;
    try { return new Intl.DateTimeFormat(CAI18N.lang, { weekday: 'short', timeZone: 'UTC' }).format(new Date(Date.UTC(2024, 0, 1 + i))); } catch (e) { return code; }
  }
  // Day names are joined with the language's own list separator (a list of items, not a sentence).
  function touDays(days) {
    var names = days.map(dayName);
    try { return new Intl.ListFormat(CAI18N.lang, { type: 'unit', style: 'short' }).format(names); } catch (e) { return names.join(', '); }
  }
  // "HH:MM[:SS]" -> the viewer's clock (24 h unless the user's Home Assistant profile says 12 h)
  function touClock(hhmmss) {
    var m = /^(\d{1,2}):(\d{2})/.exec(String(hhmmss || ''));
    if (!m) return String(hhmmss || '').slice(0, 5);
    var d = new Date(2000, 0, 1, Number(m[1]), Number(m[2]));
    var loc = CAI18N.locale;
    if (loc && loc.time_format === '12') return CAI18N.fmt.time(d);
    try { return new Intl.DateTimeFormat(CAI18N.lang, { hour: '2-digit', minute: '2-digit', hourCycle: 'h23' }).format(d); } catch (e) { return String(hhmmss).slice(0, 5); }
  }

  function renderTou() {
    var sec = $('set-tou');
    sec.hidden = !X;
    if (!X) return;
    var list = X.schedules || [];
    $('set-tou-body').innerHTML = list.length ? '<div class="scroll-x"><table style="min-width:420px"><thead><tr><th>' + esc(T('tou.name')) + '</th><th>' + esc(T('tou.days')) + '</th><th>' + esc(T('tou.window')) + '</th><th>' + esc(T('tou.cost')) + '</th></tr></thead><tbody>' + list.map(function (s) {
      return '<tr><td><bdi>' + esc(s.name || T('tou.unnamed')) + '</bdi></td><td>' + esc(touDays(s.days || [])) + '</td><td class="num"><bdi>' + esc(touClock(s.start)) + '–' + esc(touClock(s.end)) +
        '</bdi></td><td>' + esc(s.cost_tag === 'high' ? T('tou.cost_high') : s.cost_tag === 'low' ? T('tou.cost_low') : s.cost_tag || '') + '</td></tr>';
    }).join('') + '</tbody></table></div>' : '<div class="empty">' + esc(T('tou.none')) + '</div>';
  }

  var AI_STATUS = { active: 1, disabled: 1, circuit_open: 1, budget_exceeded: 1, inactive: 1, error: 1 };
  var AI_BREAKER = { closed: 1, open: 1, half_open: 1 };
  function renderIntegrations() {
    var h = '';
    if (X && X.github) {
      var g = X.github;
      h += '<div class="kv"><div>' + esc(T('github.title')) + '</div><div class="num" style="font-weight:600">' + esc(T(g.configured ? 'github.set_up' : 'github.not_set_up')) + '</div><div class="raw">' +
        esc(g.configured ? T('github.configured', { repo: iso(g.repo), token: g.token_set ? 'set' : 'missing' }) :
        (g.repo ? T('github.repo_no_token', { repo: iso(g.repo) }) : T('github.add_token'))) + '</div></div>';
    }
    var a = AI && AI.status;
    if (a) {
      // status and breaker state are codes from claude_api.py; an unknown code is shown as sent
      var st = a.status || 'unknown', br = a.circuit_breaker_state || 'closed';
      var stText = AI_STATUS[st] ? T('ai.status.' + st) : String(st).toUpperCase();
      var brText = AI_BREAKER[br] ? T('ai.breaker.' + br) : String(br);
      var cost = CAI18N.fmt.number(a.monthly_cost_estimate || 0, { style: 'currency', currency: 'USD', minimumFractionDigits: 2, maximumFractionDigits: 2 });
      var params = { requests: a.total_requests || 0, errors: a.error_count || 0, cost: cost, breaker: brText, model: iso(a.model || '') };
      h += '<div class="kv"><div>' + esc(T('ai.title')) + '</div><div class="num" style="font-weight:600">' + esc(stText) + '</div><div class="raw">' +
        esc(T(a.model ? 'ai.line_model' : 'ai.line', params)) + '</div></div>';
    }
    $('set-int-body').innerHTML = h || '<div class="empty">' + esc(T('integrations.unavailable')) + '</div>';
    $('set-foot').textContent = T(X ? 'foot.full' : 'foot.basic');
  }

  async function load() {
    var r = await Promise.allSettled([ND.api('config'), ND.api('settings_extra'), ND.api('ai_status')]);
    C = r[0].status === 'fulfilled' ? (r[0].value.settings || []) : null; cfgErr = r[0].status === 'rejected' ? r[0].reason.message : null;
    X = r[1].status === 'fulfilled' ? r[1].value : null; AI = r[2].status === 'fulfilled' ? r[2].value : null;
    loaded = true; render();
  }

  function init() {
    var d = ND.ls.get(DESC_KEY); $('set-desc').checked = d === '1';
    $('set-search').addEventListener('input', render);
    $('set-desc').addEventListener('change', function () { ND.ls.set(DESC_KEY, this.checked ? '1' : '0'); render(); });
    $('set-changed').addEventListener('change', render);
  }
  ND.register('settings', { show: function () { load(); }, refresh: load });
  ND.on('init', init);
  ND.on('zone', function () { loaded = false; if (ND.active === 'settings') load(); });
  ND.on('refresh', function () { if (ND.active === 'settings') load(); });
  ND.SETTING_GROUPS = G;
})();
