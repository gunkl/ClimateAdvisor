const http = require('http');
const fs = require('fs');
const path = require('path');
const url = require('url');

const PORT = 7531;
const FRONTEND_DIR = path.join(__dirname, '../../custom_components/climate_advisor/frontend');

function makeChartFixture() {
  const now = Date.now();

  // 24 predicted hourly points (hour 0..23)
  const predicted_outdoor = Array.from({length:24}, (_,h) => ({ hour: h, temp: 55 + 20*Math.sin((h-6)*Math.PI/12) }));
  const predicted_indoor  = Array.from({length:24}, (_,h) => ({ hour: h, temp: 68 + 4*Math.sin((h-14)*Math.PI/12) }));

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

  return {
    predicted_outdoor, predicted_indoor,
    forecast_outdoor,
    actual_outdoor, actual_indoor,
    state_log,
    comfort_heat: 68, comfort_cool: 74,
    current_hour: new Date().getHours() + new Date().getMinutes()/60,
    thermal_model: { confidence:'medium', observation_count_heat:8, observation_count_cool:5, heating_rate:2.1, cooling_rate:1.8, unit:'fahrenheit' }
  };
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
  if (pathname === '/') {
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

  // Serve frontend JS files
  const jsFiles = ['/hammer.min.js', '/chart.umd.min.js', '/chartjs-plugin-zoom.min.js'];
  if (jsFiles.includes(pathname)) {
    try {
      const filename = pathname.slice(1);
      const data = fs.readFileSync(path.join(FRONTEND_DIR, filename));
      res.writeHead(200, { 'Content-Type': 'application/javascript' });
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
