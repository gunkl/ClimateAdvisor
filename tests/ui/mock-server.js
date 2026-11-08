const http = require('http');
const fs = require('fs');
const path = require('path');
const url = require('url');

const PORT = Number(process.env.MOCK_PORT) || 7531;
const FRONTEND_DIR = path.join(__dirname, '../../custom_components/climate_advisor/frontend');

function makeChartFixture() {
  const now = Date.now();

  // 120 predicted hourly points (future-only, ts-keyed, 5 days)
  const predicted_indoor = Array.from({length:120}, (_,i) => ({
    ts: new Date(now + (i + 1) * 3600000).toISOString(),
    temp: parseFloat((68 + 4 * Math.sin((i - 14) * Math.PI / 12)).toFixed(1))
  }));

  // 5 days of future hourly forecast (absolute timestamps, 120 entries)
  const forecast_outdoor = Array.from({length:120}, (_,i) => ({
    ts: new Date(now + i * 3600000).toISOString(),
    temp: parseFloat((55 + 20*Math.sin((i - 6) * Math.PI / 12)).toFixed(1))
  }));

  // 48 actual points (every 30 min over last 24h)
  const actual_outdoor = Array.from({length:48}, (_,i) => ({
    time: new Date(now - (47-i)*1800000).toISOString(),
    temp: 52 + 18*Math.sin((i/48)*Math.PI)
  }));
  const actual_indoor = Array.from({length:48}, (_,i) => ({
    time: new Date(now - (47-i)*1800000).toISOString(),
    temp: 69 + 3*Math.sin((i/48)*Math.PI*2)
  }));

  // 48 state_log entries (every 30 min)
  const hvacStates = ['off','heating','off','cooling','off','fan'];
  const state_log = Array.from({length:48}, (_,i) => ({
    ts: new Date(now - (47-i)*1800000).toISOString(),
    hvac: hvacStates[i % hvacStates.length],
    fan: i % 7 === 0,
    indoor: 69 + 3*Math.sin((i/48)*Math.PI*2),
    outdoor: 52 + 18*Math.sin((i/48)*Math.PI),
    pred_outdoor: 55 + 20*Math.sin(((i/48) - 0.25)*Math.PI),
    pred_indoor: 68 + 4*Math.sin(((i/48) - 0.58)*Math.PI),
    windows_open: i >= 16 && i < 28,
    windows_recommended: i >= 12 && i < 30,
  }));

  // Issue #1436: the older hour-by-hour predictor said "windows recommended, fan active" for every hour ahead (the
  // owner's case). The chart must not draw any strip from it: the faded strips come from the timeline's `strips`.
  const predicted_activity = Array.from({length:48}, (_,i) => ({
    ts: new Date(now + i * 3600000).toISOString(), hvac_mode: 'off', fan_active: true, windows_recommended: true,
  }));

  return {
    predicted_indoor,
    forecast_outdoor,
    actual_outdoor, actual_indoor,
    state_log,
    predicted_activity,
    comfort_heat: 68, comfort_cool: 74,
    current_hour: new Date().getHours() + new Date().getMinutes()/60,
    thermal_model: { confidence:'medium', observation_count_heat:8, observation_count_cool:5, heating_rate:2.1, cooling_rate:1.8, unit:'fahrenheit' }
  };
}

// The operation timeline (R3) as GET /api/climate_advisor/timeline returns it: steps relative to now, display unit.
function makeTimelineFixture() {
  const now = Date.now();
  const at = (min) => new Date(now + min * 60000).toISOString();
  // Like operation_timeline._fmt_time: "8:30 PM", with the weekday on another day ("Sun 6:15 AM"). The backend's
  // sentences open with it; text_short (timeline_surfaces.plan_list_text) is the plan-list row wording without it.
  const clock = (min) => {
    const d = new Date(now + min * 60000), h = d.getHours() % 12 || 12, m = String(d.getMinutes()).padStart(2, '0');
    const t = h + ':' + m + ' ' + (d.getHours() < 12 ? 'AM' : 'PM');
    return d.toDateString() === new Date(now).toDateString() ? t : d.toLocaleDateString('en-US', { weekday: 'short' }) + ' ' + t;
  };
  const fill = (min, text) => text.replace('{t}', clock(min)).replace('{wake}', clock(1500));
  const step = (min, actor, kind, text, extra) => {
    const s = Object.assign({
      ts: at(min), actor, kind, text: fill(min, text), reason: 'test.' + kind, target_f: null, mode: null, confidence: 'ok',
      lead_minutes: actor === 'you' ? 30 : 0, source: 'test', meta: { id: kind + min, status: 'planned', facts: {} },
    }, extra || {});
    if (s.text_short) s.text_short = fill(min, s.text_short);
    return s;
  };
  return {
    schema_version: 1, zone: 'mock', generated_at: at(-5), day: new Date(now).toISOString().slice(0, 10),
    horizon_end: at(48 * 60), latched: true, regen_reason: null, unit: 'fahrenheit',
    steps: [
      step(-120, 'ca', 'setback_end', 'Past step that must not be listed.', { meta: { id: 'past', status: 'done', facts: {} } }),
      step(45, 'you', 'windows_open', '{t}: open the windows. Outdoor is 6°F cooler than the house.', { text_short: 'Open the windows. Outdoor is 6°F cooler than the house.' }),
      step(150, 'ca', 'precool_start', '{t}: CA pre-cools the house to 70°F so tomorrow\'s heat starts from a cooler house.', { text_short: 'Pre-cools the house to 70°F so tomorrow\'s heat starts from a cooler house.' }),
      step(240, 'you', 'windows_close', '{t}: close the windows. Outdoor stops helping (it is warming past the house).', { confidence: 'low', text_short: 'Close the windows. Outdoor stops helping (it is warming past the house).' }),
      step(300, 'ca', 'briefing_send', '{t}: CA sends the daily briefing.', { text_short: 'Sends the daily briefing.' }),
      step(600, 'ca', 'sleep_band', '{t}: bedtime, CA switches to the sleep band, 66°F to 72°F, and reads the bedroom sensor until {wake}.', { text_short: 'Bedtime: switches to the sleep band, 66°F to 72°F, and reads the bedroom sensor until {wake}.' }),
      step(1500, 'ca', 'wake_band', '{t}: CA switches to the daytime comfort band, 68°F to 74°F, and reads the thermostat again.', { text_short: 'Switches to the daytime comfort band, 68°F to 74°F, and reads the thermostat again.' }),
    ],
    path: Array.from({ length: 25 }, (_, i) => ({ ts: at(i * 60), temp_f: 70 + 2 * Math.sin(i / 4), sensor: '_ca', model: 'v3' })),
    // Issue #1436: the chart's faded strips, as timeline_surfaces.strip_spans derives them from the steps above: the
    // window period from the +45 min open to the +240 min close (the hover names the open step while it is ahead), and
    // one predicted heat run overnight (detail.runs; it has no step, so its own sentence).
    strips: [
      // a ~20 min heat run in a 2 h window (+660 to +780 min), drawn as the plan's per-step minutes (bursts)
      // plus one 20-second sliver at +720 (drawn at its minimum visible width, owner decision)
      ...[[675, 6], [705, 9], [720, 1 / 3], [750, 5]].map(([m, len]) => ({ row: 'heating', start: at(m), end: at(m + len), kind: 'run',
        step_id: '', step_ts: null, actor: 'ca', confidence: 'ok', run: 0, window_start: at(660), window_end: at(780),
        minutes: 20.3, placed: true,
        text: 'Heating expected for about 20 min between ' + clock(660) + ' and ' + clock(780) + ", holding the band's floor.",
        text_short: 'Heating expected for about 20 min between ' + clock(660) + ' and ' + clock(780) + ", holding the band's floor." })),
      { row: 'windowsplan', start: at(45), end: at(240), kind: 'windows_open', step_id: 'windows_open45', confidence: 'ok',
        step_ts: at(45), actor: 'you', text: fill(45, '{t}: open the windows. Outdoor is 6°F cooler than the house.'),
        text_short: 'Open the windows. Outdoor is 6°F cooler than the house.' },
    ],
  };
}

// GET /api/climate_advisor/night_strategy: NightStrategy.to_dict(), display unit, `unit` added (see the R6 backend lane).
function makeNightFixture(showNumbers) {
  const now = Date.now();
  const at = (min) => new Date(now + min * 60000).toISOString();
  const path = (base, amp) => Array.from({ length: 33 }, (_, i) => [at(i * 15), +(base + amp * Math.sin(i / 6)).toFixed(2)]);
  const opt = (key, label, cls, base, amp, actions, extra) => Object.assign({
    key, label, class: cls, bedroom_low_f: base - amp, bedroom_high_f: base + amp, bedroom_wake_f: base - 1,
    discomfort_fh: 0, heat_min: 20, cool_min: 0, hvac_minutes: 20, score: 0.5, confidence: 'high',
    n_actions: actions.length, runs: true, note: '', actions, path: path(base, amp),
  }, extra || {});
  const gate = (o) => showNumbers ? o : Object.assign({}, o, { bedroom_low_f: null, bedroom_high_f: null, bedroom_wake_f: null, discomfort_fh: null, heat_min: null, cool_min: null, hvac_minutes: null, score: null, path: [] });
  const res = {
    schema_version: 1, unit: 'fahrenheit', start: at(30), end: at(8 * 60), floor_f: 66, ceiling_f: 72,
    target_sensor: 'sensor.bedroom', fan_mode: 'whole_house_fan', recommended: 'closed', show_numbers: showNumbers,
    source_model: 'v4',
    source_note: "From v4. Tonight's bedroom plan is always built on v4's learned rates, whichever prediction model the Status chart shows.",
    headline: 'Keep the windows closed tonight; CA keeps the heat for the bedroom at 66°F.',
    tradeoffs: showNumbers ? [{ key: 'windows_2h', label: 'Windows open 2 h', score_delta: 0.4, hvac_minutes_delta: 10, discomfort_fh_delta: 0, text: 'Windows open 2 h: 10 more minutes of heat or AC, 2 more thing(s) for you to do' }] : [],
    options: [
      opt('closed', 'Keep closed', 'closed', 67, 1.5, []),
      opt('windows_2h', 'Windows open 2 h', 'windows', 66.5, 2, [
        { ts: at(60), kind: 'windows_open', actor: 'you', predicted: false, through_night: false },
        { ts: at(180), kind: 'windows_close', actor: 'you', predicted: false, through_night: false },
      ], { score: 0.9, heat_min: 30, hvac_minutes: 30 }),
    ],
  };
  res.options = res.options.map(gate);
  return res;
}

const server = http.createServer((req, res) => {
  const parsedUrl = url.parse(req.url, true);
  const pathname = parsedUrl.pathname;

  // CORS headers
  res.setHeader('Access-Control-Allow-Origin', '*');
  res.setHeader('Access-Control-Allow-Headers', 'Authorization, Content-Type');

  if (req.method === 'OPTIONS') {
    res.writeHead(204);
    res.end();
    return;
  }

  // Serve index.html — inject a mock auth token so initLoad() fires immediately
  if (pathname === '/' || pathname === '/index.html') {
    try {
      let html = fs.readFileSync(path.join(FRONTEND_DIR, 'index.html'), 'utf8');
      // Inject script before </head> so localStorage has a token before the IIFE runs
      const authScript = `<script>
try { localStorage.setItem('hassTokens', JSON.stringify({access_token:'mock-token'})); } catch(e) {}
</script>`;
      html = html.replace('</head>', authScript + '\n</head>');
      res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' });
      res.end(html);
    } catch (e) {
      res.writeHead(500, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ error: e.message }));
    }
    return;
  }

  // The new dashboard (R5/R6fe), same mock token
  if (pathname === '/new_dashboard.html') {
    try {
      let html = fs.readFileSync(path.join(FRONTEND_DIR, 'new_dashboard.html'), 'utf8');
      const authScript = `<script>
try { localStorage.setItem('hassTokens', JSON.stringify({access_token:'mock-token'})); } catch(e) {}
</script>`;
      html = html.replace('</head>', authScript + '\n</head>');
      res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' });
      res.end(html);
    } catch (e) {
      res.writeHead(500, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ error: e.message }));
    }
    return;
  }

  // Serve frontend JS files
  const jsFiles = ['/hammer.min.js', '/chart.umd.min.js', '/chartjs-plugin-zoom.min.js', '/chart_v2.js'];
  const ndFiles = /^\/(nd_[a-z_]+\.(js|json)|chart_v2\.css|prediction_accuracy\.html|entry\.html)$/;
  if (jsFiles.includes(pathname) || ndFiles.test(pathname)) {
    try {
      const filename = pathname.slice(1);
      const data = fs.readFileSync(path.join(FRONTEND_DIR, filename));
      const types = { '.js': 'application/javascript', '.css': 'text/css', '.json': 'application/json', '.html': 'text/html' };
      res.writeHead(200, { 'Content-Type': types[path.extname(filename)] || 'application/octet-stream' });
      res.end(data);
    } catch (e) {
      res.writeHead(404, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({}));
    }
    return;
  }

  // API routes
  if (pathname === '/api/climate_advisor/chart_data') {
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify(makeChartFixture()));
    return;
  }

  if (pathname === '/api/climate_advisor/status') {
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({
      day_type: 'mild',
      hvac_mode: 'off',
      comfort_heat: 68,
      comfort_cool: 74,
      indoor_temp: 71,
      outdoor_temp: 65,
      automation_enabled: true,
      occupancy_mode: 'home'
    }));
    return;
  }

  if (pathname === '/api/climate_advisor/timeline') {
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify(makeTimelineFixture()));
    return;
  }

  if (pathname === '/api/climate_advisor/night_strategy') {
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify(makeNightFixture(parsedUrl.query.numbers !== '0')));
    return;
  }

  if (pathname === '/api/climate_advisor/briefing') {
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ briefing: 'Test briefing' }));
    return;
  }

  // Unknown API routes → 200 with empty object (avoids console errors in tests)
  if (pathname.startsWith('/api/')) {
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({}));
    return;
  }

  // All other routes → 404
  res.writeHead(404, { 'Content-Type': 'application/json' });
  res.end(JSON.stringify({}));
});

server.listen(PORT, () => {
  console.log(`Mock server listening on http://localhost:${PORT}`);
});
