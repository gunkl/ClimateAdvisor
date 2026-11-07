/* Issue #1230 R5: Settings tab. Read-only, exactly like classic: it reads /config (and the new read-only
   /settings_extra for changed-from-default, TOU schedules and GitHub status) and sends the owner to Home
   Assistant's own options flow to change anything. No write path exists here. */
(function () {
  'use strict';
  var $ = ND.$, esc = ND.esc, C = null, X = null, AI = null, loaded = false, cfgErr = null;
  var DESC_KEY = 'climate_advisor_nd_show_desc';

  var G = [
    ['comfort', 'Comfort & schedule', 'Temperatures CA aims for, and when you wake and sleep.', ['comfort_heat', 'comfort_cool', 'sleep_heat', 'sleep_cool', 'setback_heat', 'setback_cool', 'wake_time', 'sleep_time']],
    ['equipment', 'Equipment', 'The thermostat, weather source and fan CA controls.', ['climate_entity', 'weather_entity', 'temp_unit', 'fan_mode', 'fan_entity', 'fan_remote_entity', 'fan_state_entity', 'fan_state_feedback', 'hvac_fan_restrict_mode', 'nat_vent_soft_start_enabled', 'fan_min_runtime_per_hour']],
    ['sensors', 'Sensors', 'Where CA reads temperatures, doors and windows.', ['indoor_temp_source', 'outdoor_temp_source', 'thermostat_companion_sensor', 'sleep_indoor_temp_entity', 'door_window_sensors', 'sensor_polarity_inverted']],
    ['occupancy', 'Occupancy', 'How CA knows you are home, away, on vacation or have guests.', ['home_toggle_entity', 'home_toggle_invert', 'vacation_toggle_entity', 'vacation_toggle_invert', 'vacation_heat', 'vacation_cool', 'guest_toggle_entity', 'guest_toggle_invert']],
    ['timing', 'Timing & overrides', 'How long CA waits before acting, and how long it backs off after you change something.', ['sensor_debounce_seconds', 'welcome_home_debounce_seconds', 'override_confirm_seconds', 'manual_grace_seconds', 'automation_grace_seconds', 'comfort_mode_switch_min_interval_s', 'comfort_family_recency_window_min']],
    ['notify', 'Briefing & notifications', 'What CA tells you, when, and how.', ['briefing_time', 'briefing_notifications_enabled', 'notify_service', 'push_briefing', 'email_briefing', 'push_door_window_pause', 'email_door_window_pause', 'push_occupancy_home', 'email_occupancy_home', 'push_entity_health', 'email_entity_health', 'manual_grace_notify', 'automation_grace_notify', 'email_grace_expired', 'email_grace_repause']],
    ['learning', 'Learning & prediction', 'What CA learns about your house and how it uses it.', ['learning_enabled', 'weather_bias_enabled', 'adaptive_setback_enabled', 'max_setback_depth_f', 'adaptive_preheat_enabled', 'default_preheat_minutes', 'min_preheat_minutes', 'max_preheat_minutes', 'preheat_safety_margin', 'default_tou_lead_minutes']],
    ['daytypes', 'Day types & savings', 'Forecast highs that set each day type, tolerances, and the savings mode.', ['threshold_hot', 'threshold_warm', 'threshold_mild', 'threshold_cool', 'comfort_deadband_hot_f', 'comfort_deadband_warm_f', 'comfort_deadband_mild_f', 'comfort_deadband_cool_f', 'comfort_deadband_cold_f', 'aggressive_savings']],
    ['ai', 'AI', 'The AI briefing writer and investigator.', ['ai_enabled', 'ai_api_key', 'ai_model', 'ai_reasoning_effort', 'ai_max_tokens', 'ai_temperature', 'ai_monthly_budget', 'ai_auto_requests_per_day', 'ai_manual_requests_per_day', 'ai_investigator_enabled', 'ai_investigator_requests_per_day']]
  ];

  function fmt(s) {
    var v = s.value;
    if (v === true) return 'On'; if (v === false) return 'Off'; if (v == null || v === '') return 'Not set';
    if (Array.isArray(v)) return v.length ? v.join(', ') : 'None';
    if (typeof v === 'object') return JSON.stringify(v);
    var lab = s.label || '', mins = function (m) { return m >= 120 && m % 60 === 0 ? (m / 60) + ' h' : m + ' min'; };
    if (typeof v === 'number' && /\((minutes|min)\)/i.test(lab)) return mins(v);
    if (typeof v === 'number' && /\((sec|seconds|s)\)/i.test(lab)) return v >= 120 ? mins(v / 60) : v + ' s';
    return String(v) + (s.unit ? (s.unit === 'fahrenheit' || s.unit === '°F' ? '°F' : ' ' + s.unit) : '');
  }
  function defaultText(key, s) {
    var c = X && X.changed && X.changed[key]; if (!c) return '';
    var d = c.default; if (d === true) d = 'On'; else if (d === false) d = 'Off';
    return 'default: ' + d + (c.unit ? (c.unit === '°F' || c.unit === '°C' ? c.unit : ' ' + c.unit) : (s.unit && typeof d === 'number' ? s.unit : ''));
  }

  function render() {
    var host = $('set-groups');
    if (!C) { host.innerHTML = '<div class="err">Failed to load settings: ' + esc(cfgErr || 'no data') + '</div>'; $('set-count').textContent = ''; return; }
    var q = ($('set-search').value || '').toLowerCase(), showDesc = $('set-desc').checked, onlyChanged = $('set-changed').checked;
    var by = {}, used = {}, groups = [], total = 0;
    C.forEach(function (s) { by[s.key] = s; });
    function add(id, title, blurb, keys) {
      var rows = keys.filter(function (k) { return by[k]; }).map(function (k) { used[k] = 1; return by[k]; }).filter(function (s) {
        if (onlyChanged && !(X && X.changed && X.changed[s.key])) return false;
        return !q || (s.label + ' ' + fmt(s) + ' ' + (s.description || '')).toLowerCase().indexOf(q) >= 0;
      });
      if (rows.length) { groups.push({ id: id, title: title, blurb: blurb, rows: rows }); total += rows.length; }
    }
    G.forEach(function (g) { add(g[0], g[1], g[2], g[3]); });
    add('other', 'Other', 'Settings not yet placed in a group.', Object.keys(by).filter(function (k) { return !used[k]; }));
    host.innerHTML = groups.map(function (g) {
      return '<section class="card" id="settings-' + g.id + '" tabindex="-1" style="padding:10px 12px;gap:4px"><h2 style="font-size:15px">' + esc(g.title) + '</h2><div class="sub">' + esc(g.blurb) + '</div>' +
        g.rows.map(function (s) {
          var def = defaultText(s.key, s);
          return '<div style="border-top:1px solid var(--rule);padding:6px 0"><div style="display:flex;justify-content:space-between;gap:12px;font-size:13px"><span>' + esc(s.label) +
            (def ? ' <span class="badge changed">changed</span>' : '') + '</span><span class="num" style="font-weight:600;text-align:right;word-break:break-word">' + esc(fmt(s)) + '</span></div>' +
            (def ? '<div class="sub" style="text-align:right">' + esc(def) + '</div>' : '') +
            (showDesc && s.description ? '<div class="sub" style="margin-top:2px;line-height:1.4">' + esc(s.description) + '</div>' : '') + '</div>';
        }).join('') + '</section>';
    }).join('') || '<div class="empty">No setting matches.</div>';
    $('set-jump').innerHTML = groups.map(function (g) { return '<a class="chip-link" href="#settings/' + g.id + '">' + esc(g.title) + '<span class="n">' + g.rows.length + '</span></a>'; }).join('');
    var nChanged = X && X.changed ? Object.keys(X.changed).length : null;
    $('set-count').textContent = total + ' of ' + C.length + ' settings · read-only here' + (nChanged != null ? ' · ' + nChanged + ' changed from default' : '');
    $('set-changed').disabled = !X;
    renderTou(); renderIntegrations();
  }

  function renderTou() {
    var sec = $('set-tou');
    sec.hidden = !X;
    if (!X) return;
    var list = X.schedules || [], dn = { mon: 'Mon', tue: 'Tue', wed: 'Wed', thu: 'Thu', fri: 'Fri', sat: 'Sat', sun: 'Sun', all: 'Every day' };
    $('set-tou-body').innerHTML = list.length ? '<div class="scroll-x"><table style="min-width:420px"><thead><tr><th>Name</th><th>Days</th><th>Window</th><th>Cost</th></tr></thead><tbody>' + list.map(function (s) {
      return '<tr><td>' + esc(s.name || '(unnamed)') + '</td><td>' + esc((s.days || []).map(function (d) { return dn[d] || d; }).join(', ')) + '</td><td class="num">' + esc(String(s.start || '').slice(0, 5)) + '–' + esc(String(s.end || '').slice(0, 5)) +
        '</td><td>' + esc(s.cost_tag === 'high' ? 'High (peak)' : s.cost_tag === 'low' ? 'Low (off-peak)' : s.cost_tag || '') + '</td></tr>';
    }).join('') + '</tbody></table></div>' : '<div class="empty">No time-of-use schedules are set up.</div>';
  }

  function renderIntegrations() {
    var h = '';
    if (X && X.github) {
      var g = X.github;
      h += '<div class="kv"><div>GitHub issues</div><div class="num" style="font-weight:600">' + (g.configured ? 'Set up' : 'Not set up') + '</div><div class="raw">' +
        (g.configured ? 'Repository ' + esc(g.repo) + ', access token ' + (g.token_set ? 'set' : 'missing') + '. Analysis can file reports as issues.' :
        (g.repo ? 'Repository ' + esc(g.repo) + ' is set but ' + (g.token_set ? '' : 'no access token is set') + '.' : 'Add a token and repository in Home Assistant (Configure, GitHub integration) so Analysis can file issues.')) + '</div></div>';
    }
    var a = AI && AI.status;
    if (a) h += '<div class="kv"><div>AI</div><div class="num" style="font-weight:600">' + esc(String(a.status || 'unknown').toUpperCase()) + '</div><div class="raw">' + (a.model ? 'Model ' + esc(a.model) + ' · ' : '') +
      (a.total_requests || 0) + ' requests · ' + (a.error_count || 0) + ' errors · $' + (a.monthly_cost_estimate || 0).toFixed(2) + ' this month · circuit breaker ' + esc(a.circuit_breaker_state || 'closed') + '</div></div>';
    $('set-int-body').innerHTML = h || '<div class="empty">Integration status is not available right now.</div>';
    $('set-foot').textContent = X ? 'These are the current settings. They are read-only here; use Change in Home Assistant to edit them.' :
      'These are the current settings, read-only. Changed-from-default markers, time-of-use schedules and GitHub status appear once Climate Advisor is updated to serve them.';
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
