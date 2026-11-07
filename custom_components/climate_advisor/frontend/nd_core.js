/* Issue #1230 R5 - new dashboard shell: auth, zone-aware API helpers, tabs, banners, health line, Actions menu.
   Same auth and zone handling as the classic dashboard (index.html): HA token from the parent window or
   localStorage 'hassTokens', one 401 retry, the stored zone key shared with classic, entry_id on every call. */
(function () {
  'use strict';

  var ND = window.ND = { unit: 'fahrenheit', status: null, zone: null, active: null, mods: {}, handlers: {} };
  var ZONE_KEY = 'climate_advisor_selected_zone'; // same key as classic and the accuracy page
  var PREF_KEY = 'climate_advisor_dashboard';     // 'new' | 'classic' (remembered per browser/user)
  var TABS = ['status', 'analysis', 'debug', 'settings'];
  var HA_CONFIG_URL = '/config/integrations/integration/climate_advisor';
  ND.TABS = TABS;
  ND.HA_CONFIG_URL = HA_CONFIG_URL;

  // ---------- small helpers ----------
  ND.$ = function (id) { return document.getElementById(id); };
  ND.esc = function (s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  };
  ND.ls = {
    get: function (k) { try { return localStorage.getItem(k); } catch (e) { return null; } },
    set: function (k, v) { try { localStorage.setItem(k, v); } catch (e) { /* private mode */ } },
    del: function (k) { try { localStorage.removeItem(k); } catch (e) { /* private mode */ } }
  };
  ND.on = function (ev, fn) { (ND.handlers[ev] = ND.handlers[ev] || []).push(fn); };
  ND.emit = function (ev, arg) { (ND.handlers[ev] || []).forEach(function (fn) { try { fn(arg); } catch (e) { console.error(e); } }); };
  ND.sym = function (u) { return u === 'celsius' || u === '°C' ? '°C' : '°F'; };
  ND.isC = function () { return ND.sym(ND.unit) === '°C'; };
  // internal records keep degrees F (suffix _f); show them in the install's unit
  ND.fToDisp = function (f) { return f == null ? null : (ND.isC() ? (f - 32) * 5 / 9 : f); };
  ND.dFToDisp = function (f) { return f == null ? null : (ND.isC() ? f * 5 / 9 : f); };
  ND.num = function (v, d) { return v == null || isNaN(v) ? '—' : Number(v).toFixed(d == null ? 1 : d); };
  ND.fmtTime = function (iso) {
    if (!iso) return '—';
    var d = new Date(iso);
    return isNaN(d) ? String(iso) : d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
  };
  ND.fmtDateTime = function (iso) {
    if (!iso) return '—';
    var d = new Date(iso);
    return isNaN(d) ? String(iso) : d.toLocaleString([], { month: 'numeric', day: 'numeric', hour: 'numeric', minute: '2-digit' });
  };
  ND.toast = function (msg, kind) {
    var t = ND.$('toast');
    t.textContent = (kind === 'err' ? 'Error: ' : '') + msg;
    t.hidden = false;
    clearTimeout(ND._toastT);
    ND._toastT = setTimeout(function () { t.hidden = true; }, kind === 'err' ? 7000 : 3500);
  };
  ND.download = function (content, filename, mime) {
    var blob = new Blob([content], { type: mime || 'text/plain' });
    var url = URL.createObjectURL(blob);
    var a = document.createElement('a');
    a.href = url; a.download = filename; a.style.display = 'none';
    document.body.appendChild(a); a.click();
    setTimeout(function () { document.body.removeChild(a); URL.revokeObjectURL(url); }, 1000);
  };
  ND.copy = function (text) {
    try {
      navigator.clipboard.writeText(text).then(function () { ND.toast('Copied'); }, function () { fallbackCopy(text); });
    } catch (e) { fallbackCopy(text); }
  };
  function fallbackCopy(text) {
    var ta = document.createElement('textarea');
    ta.value = text; ta.style.position = 'fixed'; ta.style.opacity = '0';
    document.body.appendChild(ta); ta.select();
    try { document.execCommand('copy'); ND.toast('Copied'); } catch (e) { ND.toast('Copy failed', 'err'); }
    document.body.removeChild(ta);
  }
  ND.md = function (text) { // the classic report renderer: escaped lines, pipe tables, **bold**
    function esc(s) { return ND.esc(s); }
    function inline(s) { return s.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>'); }
    var lines = String(text || '').split('\n'), out = [], i = 0;
    while (i < lines.length) {
      var t = lines[i].trim();
      if (t.charAt(0) === '|' && t.charAt(t.length - 1) === '|' && t.length > 2) {
        var tl = [];
        while (i < lines.length && lines[i].trim().charAt(0) === '|') { tl.push(lines[i]); i++; }
        var html = '<table>', head = true;
        tl.forEach(function (l) {
          if (/^\|[\s\-:|]+\|$/.test(l.trim())) { head = false; return; }
          var cells = l.split('|').slice(1, -1).map(function (c) { return inline(esc(c.trim())); });
          html += '<tr>' + cells.map(function (c) { return head ? '<th>' + c + '</th>' : '<td>' + c + '</td>'; }).join('') + '</tr>';
        });
        out.push(html + '</table>');
      } else { out.push(inline(esc(lines[i]))); i++; }
    }
    return out.join('<br>');
  };

  // ---------- auth (as classic) ----------
  function haAuth() {
    try {
      var ha = window.parent.document.querySelector('home-assistant');
      if (ha && ha.hass && ha.hass.auth) return ha.hass.auth;
    } catch (e) { /* cross-origin or not ready */ }
    return null;
  }
  async function getToken() {
    var auth = haAuth();
    if (auth) {
      if (auth.data && auth.data.expires && auth.data.expires - Date.now() < 300000) {
        try { await auth.refreshAccessToken(); } catch (e) { /* refresh failed */ }
      }
      if (auth.data && auth.data.access_token) return auth.data.access_token;
    }
    try {
      var stored = localStorage.getItem('hassTokens');
      if (stored) { var tk = JSON.parse(stored); if (tk && tk.access_token) return tk.access_token; }
    } catch (e) { /* unavailable */ }
    return null;
  }
  async function refreshToken() {
    var auth = haAuth();
    if (auth) {
      try { await auth.refreshAccessToken(); if (auth.data && auth.data.access_token) return auth.data.access_token; } catch (e) { /* ignore */ }
    }
    return null;
  }
  ND.getToken = getToken;

  // ---------- API (zone-aware; exactly the classic requests) ----------
  ND.withZone = function (path) {
    if (!ND.zone) return path;
    return path + (path.indexOf('?') >= 0 ? '&' : '?') + 'entry_id=' + encodeURIComponent(ND.zone);
  };
  async function request(path, opts, parse) {
    opts = opts || {};
    path = ND.withZone(path);
    var token = await getToken();
    var headers = Object.assign({}, opts.headers || {});
    if (token) headers.Authorization = 'Bearer ' + token;
    var resp = await fetch('/api/climate_advisor/' + path, Object.assign({ credentials: 'same-origin' }, opts, { headers: headers }));
    if (resp.status === 401) {
      var fresh = await refreshToken();
      if (fresh && fresh !== token) {
        headers.Authorization = 'Bearer ' + fresh;
        resp = await fetch('/api/climate_advisor/' + path, Object.assign({ credentials: 'same-origin' }, opts, { headers: headers }));
      } else { throw new Error('HTTP 401'); }
    }
    if (!resp.ok) {
      var msg = 'HTTP ' + resp.status, b = null;
      try { b = await resp.json(); if (b && b.message) msg = b.message; } catch (e) { /* ignore */ }
      var err = new Error(msg); err.status = resp.status; err.body = b; // callers tell 'no plan yet' (503) from a failure
      throw err;
    }
    return parse ? resp.json() : resp;
  }
  ND.api = function (path, opts) { return request(path, opts, true); };
  ND.post = function (path, body) {
    return request(path, Object.assign({ method: 'POST', headers: { 'Content-Type': 'application/json' } },
      body !== undefined ? { body: JSON.stringify(body) } : {}), true);
  };
  ND.stream = function (path, body) { // ai_investigate (server-sent events), as classic
    return request(path, { method: 'POST', headers: { Accept: 'text/event-stream', 'Content-Type': 'application/json' }, body: JSON.stringify(body) }, false);
  };

  // ---------- zone ----------
  var zoneSig = null;
  function renderZones(zones, count) {
    var wrap = ND.$('zone-wrap'), sel = ND.$('zone-select');
    if (!zones || count <= 1) {
      wrap.hidden = true; sel.textContent = ''; zoneSig = null;
      if (ND.zone) { ND.zone = null; ND.ls.del(ZONE_KEY); }
      return false;
    }
    var sig = zones.map(function (z) { return z.entry_id; }).join(',');
    var changed = false;
    if (sig !== zoneSig) {
      zoneSig = sig;
      if (!ND.zone || !zones.some(function (z) { return z.entry_id === ND.zone; })) {
        ND.zone = zones[0].entry_id; ND.ls.set(ZONE_KEY, ND.zone); changed = true;
      }
      sel.innerHTML = zones.map(function (z) {
        return '<option value="' + ND.esc(z.entry_id) + '">' + ND.esc(z.title || z.entry_id) + '</option>';
      }).join('');
    }
    sel.value = ND.zone;
    wrap.hidden = false;
    return changed;
  }

  // ---------- status, health, banners ----------
  var MAX_RETRY = 3;
  ND.loadStatus = async function (depth) {
    depth = depth || 0;
    try {
      var data = await ND.api('status');
      if (data.zone_selection_required) {
        renderZones(data.zones, data.zone_count);
        if (depth < MAX_RETRY) return ND.loadStatus(depth + 1);
        return null;
      }
      renderZones(data.zones, data.zone_count);
      ND.status = data;
      ND.unit = data.unit || ND.unit;
      renderHealth(data);
      ND.renderBanners();
      ND.emit('status', data);
      return data;
    } catch (e) {
      if (depth < MAX_RETRY && ND.zone && e.message === 'HTTP 503') { // stale stored zone: clear and retry unscoped
        ND.zone = null; ND.ls.del(ZONE_KEY);
        return ND.loadStatus(depth + 1);
      }
      ND.status = null;
      ND.$('health-text').textContent = 'Could not load status: ' + e.message;
      ND.$('health').className = 'health bad';
      ND.renderBanners();
      return null;
    }
  };
  function renderHealth(s) {
    var ok = s.coordinator_healthy !== false;
    ND.$('health').className = 'health ' + (ok ? 'ok' : 'bad');
    ND.$('health-text').textContent = (ok ? 'Healthy' : 'Unavailable') + (s.version ? ' · v' + s.version : '') +
      ' · automation ' + (s.automation_enabled ? 'on' : 'off');
    ND.$('menu-toggle-automation').textContent = s.automation_enabled ? 'Pause all automation' : 'Resume automation';
  }

  function banner(level, title, text, actions, tabs, id) {
    return { id: id || title, level: level, title: title, text: text || '', actions: actions || [], tabs: tabs || null };
  }
  ND.buildBanners = function (s, extra) {
    var b = [];
    if (s.coordinator_healthy === false) {
      b.push(banner('bad', 'Climate Advisor is unavailable', 'Since ' + (s.stale_since ? ND.fmtTime(s.stale_since) : 'an unknown time') + ': ' + (s.last_error || 'unknown error')));
    }
    if (s.automation_enabled === false) {
      b.push(banner('warn', 'Automation is off', 'Climate Advisor is watching only (observe-only): it will not change the thermostat or fan.',
        [{ label: 'Turn automation on', act: 'toggle_automation' }]));
    }
    if (s.manual_override_active || s.grace_active) {
      var graceOnly = !s.manual_override_active && !s.fan_override_active && s.grace_active;
      b.push(banner('info', graceOnly ? 'A grace period is active' : 'You changed the thermostat',
        'Climate Advisor is hands-off until it ends.', [{ label: graceOnly ? 'End grace period' : 'Cancel override', act: 'cancel_override' }]));
    }
    if (s.fan_override_active || s.fan_off_hold_active) {
      var fanHold = !s.fan_override_active && s.fan_off_hold_active;
      b.push(banner('info', fanHold ? 'Fan-off pause is active' : 'Fan override is active', '',
        [{ label: fanHold ? 'End fan-off pause' : 'Cancel fan override', act: 'cancel_fan_override' }]));
    }
    if (s.paused_by_door) {
      b.push(banner('warn', 'Paused: a door or window is open', s.pause_suppressed_classification_text || '', [{ label: 'Resume from pause', act: 'resume_from_pause' }]));
    }
    if (s.occupancy_mode === 'vacation' || s.occupancy_mode === 'away') {
      b.push(banner('info', s.occupancy_mode === 'vacation' ? 'Vacation mode' : 'Away', 'Climate Advisor is using the ' + s.occupancy_mode + ' temperatures.'));
    }
    (s.entity_health_issues || []).forEach(function (i) {
      var weather = /weather/i.test(i.friendly_name || '');
      b.push(banner('warn', (weather ? 'Weather source problem: ' : 'Sensor problem: ') + (i.friendly_name || i.entity_id),
        (i.entity_id ? i.entity_id + ' is ' : 'is ') + (i.status || 'unavailable') + (i.detail ? ': ' + i.detail : '')));
    });
    (s.contact_sensors || []).filter(function (c) { return c.unreadable; }).forEach(function (c) {
      b.push(banner('warn', 'Sensor problem: ' + c.friendly_name, 'Not responding; last read ' + (c.last_reading || 'never') + ', treated as ' + c.treated_as + '.'));
    });
    (s.invariant_violations || []).concat(s.hvac_control_issues || []).forEach(function (v) {
      b.push(banner('bad', 'Problem', v.detail));
    });
    var ps = s.promise_summary;
    if (ps && ps.total_hits) {
      b.push(banner('info', ps.total_hits + ' household promise' + (ps.total_hits === 1 ? '' : 's') + ' missed in 24 h',
        (ps.promises || []).map(function (p) { return String(p.label || p.promise).replace(/^\d+: /, '') + ' ×' + p.hits; }).join(' · '),
        [{ label: 'Open in Analysis', href: '#analysis' }], ['status'], 'promises'));
    }
    if (extra && extra.reload_needed) {
      b.push(banner('warn', 'Saved settings are waiting for a reload', 'Reload Climate Advisor in Home Assistant to apply them.',
        [{ label: 'Open Home Assistant', href: HA_CONFIG_URL, blank: true }]));
    }
    return b;
  };
  ND.renderBanners = function () {
    var host = ND.$('banners');
    var list = ND.status ? ND.buildBanners(ND.status, ND.extra) : [];
    list = list.filter(function (b) { return !b.tabs || b.tabs.indexOf(ND.active) >= 0; });
    var sig = JSON.stringify(list);
    if (sig === ND._bannerSig) return;
    ND._bannerSig = sig;
    host.innerHTML = '';
    list.forEach(function (b) {
      var el = document.createElement('div');
      el.className = 'banner ' + b.level;
      el.setAttribute('role', b.level === 'bad' ? 'alert' : 'status');
      el.innerHTML = '<div class="b-text"><strong>' + ND.esc(b.title) + '</strong>' + (b.text ? ' ' + ND.esc(b.text) : '') + '</div>';
      b.actions.forEach(function (a) {
        var c;
        if (a.href) {
          c = document.createElement('a'); c.href = a.href; c.className = 'btn small'; c.textContent = a.label;
          c.style.cssText = 'display:inline-flex;align-items:center;text-decoration:none;color:var(--ink)';
          if (a.blank) { c.target = '_blank'; c.rel = 'noopener'; }
        } else {
          c = document.createElement('button'); c.type = 'button'; c.className = 'btn small'; c.textContent = a.label;
          c.addEventListener('click', function () { ND.runAction(a.act, null, c); });
        }
        el.appendChild(c);
      });
      host.appendChild(el);
    });
  };
  ND.loadExtra = async function () { // settings_extra: reload-needed flag (404 until the view is registered)
    try { ND.extra = await ND.api('settings_extra'); ND.extraOk = true; } catch (e) { ND.extra = null; ND.extraOk = false; }
    ND.renderBanners();
    return ND.extra;
  };

  // ---------- actions (the same POSTs as classic) ----------
  ND.runAction = async function (action, body, btn) {
    if (btn) btn.disabled = true;
    try {
      var data = await ND.post(action, body);
      ND.toast(data.message || 'Done');
      ND.emit('action', action);
      if (action === 'toggle_automation' || action === 'cancel_override' || action === 'cancel_fan_override' || action === 'resume_from_pause') ND.emit('state-changed');
      setTimeout(function () { ND.loadStatus(); }, 700);
      return data;
    } catch (e) {
      ND.toast(e.message, 'err');
      return null;
    } finally { if (btn) btn.disabled = false; }
  };
  function initMenu() {
    var btn = ND.$('actions-btn'), menu = ND.$('actions-menu');
    function close(focus) { menu.hidden = true; btn.setAttribute('aria-expanded', 'false'); if (focus) btn.focus(); }
    function open() { menu.hidden = false; btn.setAttribute('aria-expanded', 'true'); var f = menu.querySelector('button'); if (f) f.focus(); }
    btn.addEventListener('click', function () { menu.hidden ? open() : close(); });
    btn.addEventListener('keydown', function (e) { if (e.key === 'ArrowDown') { e.preventDefault(); open(); } });
    menu.addEventListener('keydown', function (e) {
      var items = Array.prototype.slice.call(menu.querySelectorAll('button')), i = items.indexOf(document.activeElement);
      if (e.key === 'Escape') { e.preventDefault(); close(true); }
      else if (e.key === 'ArrowDown') { e.preventDefault(); items[(i + 1) % items.length].focus(); }
      else if (e.key === 'ArrowUp') { e.preventDefault(); items[(i - 1 + items.length) % items.length].focus(); }
      else if (e.key === 'Tab') close();
    });
    document.addEventListener('click', function (e) { if (!menu.hidden && !menu.contains(e.target) && e.target !== btn) close(); });
    menu.addEventListener('click', function (e) {
      var it = e.target.closest('button[data-action]');
      if (!it) return;
      close(true);
      var a = it.getAttribute('data-action');
      if (a === 'refresh') { ND.loadStatus(); ND.loadExtra(); ND.emit('refresh'); ND.toast('Refreshed'); }
      else ND.runAction(a);
    });
  }

  // ---------- tabs (deep-linkable: #analysis, #settings/comfort) ----------
  ND.register = function (name, mod) { ND.mods[name] = mod; };
  ND.showTab = function (name, section, noHash) {
    if (TABS.indexOf(name) < 0) name = 'status';
    var prev = ND.active;
    ND.active = name;
    TABS.forEach(function (t) {
      var on = t === name, b = ND.$('tab-btn-' + t);
      b.setAttribute('aria-selected', on ? 'true' : 'false'); b.tabIndex = on ? 0 : -1;
      ND.$('panel-' + t).hidden = !on;
    });
    if (!noHash) {
      var h = '#' + name + (section ? '/' + section : '');
      if (location.hash !== h) { try { history.replaceState(null, '', h); } catch (e) { location.hash = h; } }
    }
    ND.renderBanners();
    var mod = ND.mods[name];
    if (mod && mod.show && prev !== name && !ND._booting) mod.show();
    if (section) {
      setTimeout(function () {
        var el = ND.$(name + '-' + section);
        if (el) { el.scrollIntoView({ block: 'start' }); if (el.tabIndex < 0) el.tabIndex = -1; el.focus({ preventScroll: true }); }
      }, 50);
    }
  };
  function parseHash() {
    var h = (location.hash || '').replace(/^#/, '').split('/');
    return { tab: TABS.indexOf(h[0]) >= 0 ? h[0] : null, section: h[1] || null };
  }
  function initTabs() {
    var btns = TABS.map(function (t) { return ND.$('tab-btn-' + t); });
    btns.forEach(function (b, i) {
      b.addEventListener('click', function () { ND.showTab(b.getAttribute('data-tab')); });
      b.addEventListener('keydown', function (e) {
        var n = -1;
        if (e.key === 'ArrowRight') n = (i + 1) % btns.length;
        else if (e.key === 'ArrowLeft') n = (i - 1 + btns.length) % btns.length;
        else if (e.key === 'Home') n = 0;
        else if (e.key === 'End') n = btns.length - 1;
        if (n >= 0) { e.preventDefault(); btns[n].focus(); ND.showTab(TABS[n]); }
      });
    });
    window.addEventListener('hashchange', function () {
      var p = parseHash();
      if (p.tab) ND.showTab(p.tab, p.section, true);
    });
  }

  // ---------- zone select + classic switch ----------
  function initZone() {
    ND.$('zone-select').addEventListener('change', function (e) {
      ND.zone = e.target.value; ND.ls.set(ZONE_KEY, ND.zone);
      ND.loadStatus(); ND.loadExtra();
      ND.emit('zone');
    });
  }
  function initSwitch() {
    ND.ls.set(PREF_KEY, 'new'); // opting in: remembered; classic stays the default for anyone who never opted in
    ND.$('back-classic').addEventListener('click', function () { ND.ls.set(PREF_KEY, 'classic'); });
  }

  // ---------- boot ----------
  ND.start = async function () {
    ND._booting = true;
    ND.zone = ND.ls.get(ZONE_KEY) || null;
    initMenu(); initTabs(); initZone(); initSwitch();
    ND.emit('init');
    var p = parseHash();
    ND.showTab(p.tab || 'status', p.section, true);
    var token = null;
    for (var i = 0; i < 20 && !token; i++) { token = await getToken(); if (!token) await new Promise(function (r) { setTimeout(r, 250); }); }
    await ND.loadStatus();
    ND.loadExtra();
    ND._booting = false;
    ND.emit('ready');
    var mod = ND.mods[ND.active];
    if (mod && mod.show) mod.show();
    if (p.section) ND.showTab(ND.active, p.section, true);
    (function poll() {
      setTimeout(async function () {
        await ND.loadStatus(); ND.loadExtra();
        ND.emit('poll');
        poll();
      }, 60000);
    })();
  };
})();
