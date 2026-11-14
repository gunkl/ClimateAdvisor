/* Issue #1230 R5: Settings tab. Read-only, exactly like classic: it reads /config (and the new read-only
   /settings_extra for changed-from-default, TOU schedules and GitHub status) and sends the owner to Home
   Assistant's own options flow to change anything. No write path exists here. */
(function () {
  'use strict';
  var T = window.CA_I18N.t;
  var $ = ND.$, esc = ND.esc, C = null, X = null, AI = null, loaded = false, cfgErr = null;
  var DESC_KEY = 'climate_advisor_nd_show_desc';

  function groups() {
    return [
      ['comfort', T('set.g_comfort'), T('set.g_comfort_blurb'), ['comfort_heat', 'comfort_cool', 'sleep_heat', 'sleep_cool', 'setback_heat', 'setback_cool', 'wake_time', 'sleep_time']],
      ['equipment', T('set.g_equipment'), T('set.g_equipment_blurb'), ['climate_entity', 'weather_entity', 'temp_unit', 'fan_mode', 'fan_entity', 'fan_remote_entity', 'fan_state_entity', 'fan_state_feedback', 'hvac_fan_restrict_mode', 'nat_vent_soft_start_enabled', 'fan_min_runtime_per_hour']],
      ['sensors', T('set.g_sensors'), T('set.g_sensors_blurb'), ['indoor_temp_source', 'outdoor_temp_source', 'thermostat_companion_sensor', 'sleep_indoor_temp_entity', 'indoor_humidity_entity', 'sleep_indoor_humidity_entity', 'outdoor_humidity_entity', 'door_window_sensors', 'sensor_polarity_inverted']],
      ['occupancy', T('set.g_occupancy'), T('set.g_occupancy_blurb'), ['home_toggle_entity', 'home_toggle_invert', 'vacation_toggle_entity', 'vacation_toggle_invert', 'vacation_heat', 'vacation_cool', 'guest_toggle_entity', 'guest_toggle_invert']],
      ['timing', T('set.g_timing'), T('set.g_timing_blurb'), ['sensor_debounce_seconds', 'welcome_home_debounce_seconds', 'override_confirm_seconds', 'manual_grace_seconds', 'automation_grace_seconds', 'comfort_mode_switch_min_interval_s', 'comfort_family_recency_window_min']],
      ['notify', T('set.g_notify'), T('set.g_notify_blurb'), ['briefing_time', 'briefing_notifications_enabled', 'notify_service', 'push_briefing', 'email_briefing', 'push_door_window_pause', 'email_door_window_pause', 'push_occupancy_home', 'email_occupancy_home', 'push_entity_health', 'email_entity_health', 'manual_grace_notify', 'automation_grace_notify', 'email_grace_expired', 'email_grace_repause']],
      ['learning', T('set.g_learning'), T('set.g_learning_blurb'), ['primary_engine', 'shadow_engine', 'learning_enabled', 'weather_bias_enabled', 'adaptive_setback_enabled', 'max_setback_depth_f', 'adaptive_preheat_enabled', 'default_preheat_minutes', 'min_preheat_minutes', 'max_preheat_minutes', 'preheat_safety_margin', 'default_tou_lead_minutes']],
      ['daytypes', T('set.g_daytypes'), T('set.g_daytypes_blurb'), ['threshold_hot', 'threshold_warm', 'threshold_mild', 'threshold_cool', 'comfort_deadband_hot_f', 'comfort_deadband_warm_f', 'comfort_deadband_mild_f', 'comfort_deadband_cool_f', 'comfort_deadband_cold_f', 'aggressive_savings', 'energy_rate', 'heat_power_kw', 'cool_power_kw']],
      ['ai', T('set.g_ai'), T('set.g_ai_blurb'), ['ai_enabled', 'ai_api_key', 'ai_model', 'ai_reasoning_effort', 'ai_max_tokens', 'ai_temperature', 'ai_monthly_budget', 'ai_auto_requests_per_day', 'ai_manual_requests_per_day', 'ai_investigator_enabled', 'ai_investigator_requests_per_day']]
    ];
  }
  var G = groups();

  function fmt(s) {
    var v = s.value;
    if (v === true) return T('set.on'); if (v === false) return T('set.off'); if (v == null || v === '') return T('set.not_set');
    if (Array.isArray(v)) return v.length ? v.join(', ') : T('set.none');
    if (typeof v === 'object') return JSON.stringify(v);
    var lab = s.label || '', mins = function (m) { return m >= 120 && m % 60 === 0 ? (m / 60) + ' ' + T('set.h') : m + ' ' + T('set.min'); };
    if (typeof v === 'number' && /\((minutes|min)\)/i.test(lab)) return mins(v);
    if (typeof v === 'number' && /\((sec|seconds|s)\)/i.test(lab)) return v >= 120 ? mins(v / 60) : v + ' ' + T('set.s');
    return String(v) + (s.unit ? (s.unit === 'fahrenheit' || s.unit === '°F' ? '°F' : ' ' + s.unit) : '');
  }
  function defaultText(key, s) {
    var c = X && X.changed && X.changed[key]; if (!c) return '';
    var d = c.default; if (d === true) d = T('set.on'); else if (d === false) d = T('set.off');
    return T('set.default_prefix') + ' ' + d + (c.unit ? (c.unit === '°F' || c.unit === '°C' ? c.unit : ' ' + c.unit) : (s.unit && typeof d === 'number' ? s.unit : ''));
  }

  function render() {
    var host = $('set-groups');
    if (!C) { host.innerHTML = '<div class="err">' + esc(T('set.failed_load', { msg: cfgErr || T('set.no_data') })) + '</div>'; $('set-count').textContent = ''; return; }
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
    add('other', T('set.g_other'), T('set.g_other_blurb'), Object.keys(by).filter(function (k) { return !used[k]; }));
    host.innerHTML = groups.map(function (g) {
      return '<section class="card" id="settings-' + g.id + '" tabindex="-1" style="padding:10px 12px;gap:4px"><h2 style="font-size:15px">' + esc(g.title) + '</h2><div class="sub">' + esc(g.blurb) + '</div>' +
        g.rows.map(function (s) {
          var def = defaultText(s.key, s);
          return '<div style="border-top:1px solid var(--rule);padding:6px 0"><div style="display:flex;justify-content:space-between;gap:12px;font-size:13px"><span>' + esc(s.label) +
            (def ? ' <span class="badge changed">' + esc(T('set.changed')) + '</span>' : '') + '</span><span class="num" style="font-weight:600;text-align:right;word-break:break-word">' + esc(fmt(s)) + '</span></div>' +
            (def ? '<div class="sub" style="text-align:right">' + esc(def) + '</div>' : '') +
            (showDesc && s.description ? '<div class="sub" style="margin-top:2px;line-height:1.4">' + esc(s.description) + '</div>' : '') + '</div>';
        }).join('') + '</section>';
    }).join('') || '<div class="empty">' + esc(T('set.no_match')) + '</div>';
    $('set-jump').innerHTML = groups.map(function (g) { return '<a class="chip-link" href="#settings/' + g.id + '">' + esc(g.title) + '<span class="n">' + g.rows.length + '</span></a>'; }).join('');
    var nChanged = X && X.changed ? Object.keys(X.changed).length : null;
    $('set-count').textContent = T('set.count_of', { total: total, all: C.length }) + (nChanged != null ? ' · ' + T('set.n_changed', { n: nChanged }) : '');
    $('set-changed').disabled = !X;
    renderTou(); renderIntegrations();
  }

  function renderTou() {
    var sec = $('set-tou');
    sec.hidden = !X;
    if (!X) return;
    var list = X.schedules || [], dn = { mon: T('set.mon'), tue: T('set.tue'), wed: T('set.wed'), thu: T('set.thu'), fri: T('set.fri'), sat: T('set.sat'), sun: T('set.sun'), all: T('set.every_day') };
    $('set-tou-body').innerHTML = list.length ? '<div class="scroll-x"><table style="min-width:420px"><thead><tr><th>' + esc(T('set.tou_name')) + '</th><th>' + esc(T('set.tou_days')) + '</th><th>' + esc(T('set.tou_window')) + '</th><th>' + esc(T('set.tou_cost')) + '</th></tr></thead><tbody>' + list.map(function (s) {
      return '<tr><td>' + esc(s.name || T('set.unnamed')) + '</td><td>' + esc((s.days || []).map(function (d) { return dn[d] || d; }).join(', ')) + '</td><td class="num">' + esc(String(s.start || '').slice(0, 5)) + '–' + esc(String(s.end || '').slice(0, 5)) +
        '</td><td>' + esc(s.cost_tag === 'high' ? T('set.cost_high') : s.cost_tag === 'low' ? T('set.cost_low') : s.cost_tag || '') + '</td></tr>';
    }).join('') + '</tbody></table></div>' : '<div class="empty">' + esc(T('set.no_tou')) + '</div>';
  }

  function renderIntegrations() {
    var h = '';
    if (X && X.github) {
      var g = X.github;
      h += '<div class="kv"><div>' + esc(T('set.github_issues')) + '</div><div class="num" style="font-weight:600">' + (g.configured ? esc(T('set.set_up')) : esc(T('set.not_set_up'))) + '</div><div class="raw">' +
        esc(g.configured ? T('set.gh_configured', { repo: g.repo, token: g.token_set ? T('set.set_lc') : T('set.missing') }) :
        (g.repo ? T('set.gh_repo_set_no_token', { repo: g.repo, extra: g.token_set ? '' : T('set.no_token') }) : T('set.gh_add_token'))) + '</div></div>';
    }
    var a = AI && AI.status;
    if (a) h += '<div class="kv"><div>' + esc(T('set.ai')) + '</div><div class="num" style="font-weight:600">' + esc(String(a.status || T('set.unknown')).toUpperCase()) + '</div><div class="raw">' + (a.model ? esc(T('set.ai_model', { model: a.model })) + ' · ' : '') +
      esc(T('set.ai_requests', { n: a.total_requests || 0 })) + ' · ' + esc(T('set.ai_errors', { n: a.error_count || 0 })) + ' · $' + (a.monthly_cost_estimate || 0).toFixed(2) + ' ' + esc(T('set.this_month')) + ' · ' + esc(T('set.circuit_breaker', { state: a.circuit_breaker_state || T('set.closed') })) + '</div></div>';
    $('set-int-body').innerHTML = h || '<div class="empty">' + esc(T('set.integration_unavailable')) + '</div>';
    $('set-foot').textContent = X ? T('settings.readonly_note') :
      T('set.readonly_note_no_extra');
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
