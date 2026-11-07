"""Real-browser check: every enabled Status-chart chip draws something when switched on (0.9.6 Lane F).

    pwvenv/bin/python tests/browser/check_chart_chips.py <repo-root> <screenshot-dir>

Occupant view: the owner switched on v4 lines that did nothing ("most do nothing, and only one shows prior history").
A chip that cannot draw must be disabled and say why; an enabled chip must change the chart. For both dashboards
(index.html, new_dashboard.html), two payloads (the live 0.9.5 Zone 1 shape, ``chart_v2_zone1_live_095.json``, and
the full mock where v4 has every sensor, ``chart_v2_zone1_24h.json``) and three views (the default 24 h, 6 h, and
scrolled back into history where the payload has nothing in view), every key chip and every past-lead button:

* disabled  -> it has a reason in its title and its accessible name;
* enabled   -> switching it on changes the pixels of the chart canvases (indoor, outdoor, strips), and a line chip's
               own datasets have a point in the view; a past-lead button gives a v4 line a past point in the view.

Also: no Humidity / Feels-like chip (Issue #1414), and in the live shape only the sensors v4 has data for get v4
chips (the rest are named on the "v4 is still learning" line, the finer sensor's chips say "history only").
Screenshots: chips-<page>-<payload>-<view>.png (the page) and key-<page>-<payload>-<view>.png (the key).
Exits 1 on any problem.
"""

from __future__ import annotations

import datetime as dt
import functools
import http.server
import json
import pathlib
import re
import sys
import threading
import urllib.parse

from playwright.sync_api import sync_playwright

ROOT = pathlib.Path(sys.argv[1])
OUT = pathlib.Path(sys.argv[2])
OUT.mkdir(parents=True, exist_ok=True)
FE = ROOT / "custom_components/climate_advisor/frontend"
FX = json.loads((ROOT / "tests/fixtures/new_dashboard_api.json").read_text())
PAYLOADS = {
    "live": json.loads((ROOT / "tests/fixtures/chart_v2_zone1_live_095.json").read_text()),
    "full": json.loads((ROOT / "tests/fixtures/chart_v2_zone1_24h.json").read_text()),
}
ISO = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}")
ENGINES = {
    "primary": "v4", "shadow": "v3", "fallback": False, "fitting": False, "primary_state": "ready", "text": "",
    "engines": [
        {"id": "v3", "label": "v3", "running": True, "primary": False, "shadow": True, "usable": True},
        {"id": "v4", "label": "v4", "running": True, "primary": True, "shadow": False, "usable": True},
    ],
}  # fmt: skip


class Quiet(http.server.ThreadingHTTPServer):
    def handle_error(self, *a):
        pass


http.server.SimpleHTTPRequestHandler.log_message = lambda *a, **k: None  # type: ignore[method-assign]
srv = Quiet(("127.0.0.1", 0), functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(FE)))
PORT = srv.server_port
threading.Thread(target=srv.serve_forever, daemon=True).start()

problems: list[str] = []
checks = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global checks
    checks += 1
    if not ok:
        problems.append(f"{name} {detail}".strip())


def shift(o, delta):
    if isinstance(o, str) and ISO.match(o):
        try:
            return (dt.datetime.fromisoformat(o) + delta).isoformat()
        except ValueError:
            return o
    if isinstance(o, list):
        return [shift(x, delta) for x in o]
    if isinstance(o, dict):
        return {k: shift(v, delta) for k, v in o.items()}
    return o


def chart_payload(which: str) -> dict:
    src = PAYLOADS[which]
    body = shift(src["chart_data"], dt.datetime.now(dt.UTC).astimezone() - dt.datetime.fromisoformat(src["now"]))
    body.setdefault("prediction_engines", ENGINES)
    return body


def open_page(browser, page: str, which: str, name: str):
    ctx = browser.new_context(viewport={"width": 1280, "height": 1000})
    ctx.add_init_script(
        "localStorage.setItem('hassTokens', JSON.stringify("
        "{access_token:'t',token_type:'Bearer',expires:Date.now()+3.6e6}))"
    )
    pg = ctx.new_page()
    pg.on(
        "console",
        lambda m: problems.append(f"{name} console.{m.type}: {m.text}") if m.type in ("error", "warning") else None,
    )
    pg.on("pageerror", lambda e: problems.append(f"{name} pageerror: {e}"))

    def route(r):
        ep = urllib.parse.urlparse(r.request.url).path.split("/api/climate_advisor/")[-1]
        body = {}
        if ep == "chart_data":
            body = chart_payload(which)
        elif ep == "status":
            body = dict(FX["status"], zone_count=1, zones=[])
        elif ep in FX:
            body = FX[ep]
        r.fulfill(status=200, content_type="application/json", body=json.dumps(body))

    pg.route("**/api/climate_advisor/**", route)
    pg.goto(f"http://127.0.0.1:{PORT}/{page}")
    pg.wait_for_function("() => window.Chart && Chart.getChart(document.getElementById('temp-chart'))", timeout=10000)
    pg.wait_for_timeout(800)
    if pg.evaluate("document.getElementById('chart-key').open") is False:
        pg.locator("#chart-key > summary").click()
        pg.wait_for_timeout(100)
    return ctx, pg


PIXELS_JS = """() => ['temp-chart', 'out-chart', 'activity-chart'].map(id => {
  const c = document.getElementById(id); if (!c) return '';
  const hidden = c.offsetParent === null || !c.width || !c.height;
  return hidden ? 'hidden' : c.toDataURL(); }).join('|')"""

# the chip's own datasets: does any have a non-null point in the view, or one segment across it?
IN_VIEW_JS = """(id) => {
  const ds = [];
  ['temp-chart', 'out-chart'].forEach(c => { const ch = Chart.getChart(document.getElementById(c));
    if (ch) ch.data.datasets.forEach((d, i) => {
      if (d.caId === id) ds.push({ d, vis: ch.isDatasetVisible(i), x: ch.scales.x }); }); });
  if (!ds.length) return null;
  return ds.some(({ d, vis, x }) => vis && d.data.some((p, i) => p.y != null && ((p.x >= x.min && p.x <= x.max) ||
    (d.data[i + 1] && d.data[i + 1].y != null && p.x < x.min && d.data[i + 1].x > x.max)))); }"""

PAST_V4_IN_VIEW_JS = """() => { const ch = Chart.getChart(document.getElementById('temp-chart'));
  const x = ch.scales.x; const now = Date.now();
  return ch.data.datasets.some((d, i) => String(d.caId).startsWith('v4:') && ch.isDatasetVisible(i) &&
    d.data.some(p => p.y != null && p.x < now && p.x >= x.min && p.x <= x.max)); }"""


def chip(pg, sid):
    return pg.locator(f'#chart-key-body [data-series="{sid}"]')


def audit(pg, name):
    ids = pg.evaluate(
        "() => [...document.querySelectorAll('#chart-key-body [data-series]')].map(b => b.dataset.series)"
    )
    check(f"{name}: the key has chips", len(ids) >= 10, str(ids))
    for hidden in ("ov:humidity", "ov:feelslike"):
        check(f"{name}: no {hidden} chip (Issue #1414)", hidden not in ids)
    key_text = pg.locator("#chart-key-body").inner_text()
    check(f"{name}: no Humidity / Feels-like in the key", "Humidity" not in key_text and "Feels" not in key_text)
    enabled = 0
    for sid in ids:
        c = chip(pg, sid)
        if c.count() != 1:
            problems.append(f"{name}: {sid} chip count {c.count()}")
            continue
        label = c.inner_text().strip()
        if c.is_disabled():
            title = c.get_attribute("title") or ""
            aria = c.get_attribute("aria-label") or ""
            check(f"{name}: disabled {sid} ({label}) says why on hover", len(title) > 10, repr(title))
            check(f"{name}: disabled {sid} ({label}) says why to a screen reader", title and title in aria, repr(aria))
            check(f"{name}: disabled {sid} ({label}) is off", c.get_attribute("aria-pressed") == "false")
            continue
        enabled += 1
        was_on = c.get_attribute("aria-pressed") == "true"
        if was_on:
            c.click()
            pg.wait_for_timeout(80)
        before = pg.evaluate(PIXELS_JS)
        chip(pg, sid).click()
        pg.wait_for_timeout(120)
        after = pg.evaluate(PIXELS_JS)
        now_on = chip(pg, sid).get_attribute("aria-pressed") == "true"
        check(f"{name}: enabled {sid} ({label}) is on after a click", now_on)
        check(f"{name}: enabled {sid} ({label}) draws something when switched on", before != after)
        in_view = pg.evaluate(IN_VIEW_JS, sid)
        if in_view is not None:
            check(f"{name}: enabled {sid} ({label}) has a point in the view", in_view)
        if not was_on:
            chip(pg, sid).click()
            pg.wait_for_timeout(60)
    # past-lead buttons: each enabled one gives a v4 line a past point in the view
    leads = pg.evaluate("() => [...document.querySelectorAll('#chart-key-body [data-lead]')].map(b => b.dataset.lead)")
    if leads:
        for sid in pg.evaluate(
            "() => [...document.querySelectorAll('#chart-key-body [data-series^=\"v4:\"]:not([disabled])')]"
            ".map(b => b.dataset.series)"
        ):
            if chip(pg, sid).get_attribute("aria-pressed") != "true":
                chip(pg, sid).click()
                pg.wait_for_timeout(60)
        for lead in leads:
            b = pg.locator(f'#chart-key-body [data-lead="{lead}"]')
            if b.is_disabled():
                check(f"{name}: disabled lead {lead} says why", len(b.get_attribute("title") or "") > 10)
                continue
            b.click()
            pg.wait_for_timeout(120)
            check(f"{name}: lead {lead} draws a past v4 line in the view", pg.evaluate(PAST_V4_IN_VIEW_JS))
    return enabled


def live_shape(pg, name):
    v4_chips = pg.evaluate(
        """() => [...document.querySelectorAll(
          '#chart-key-body [data-series^="v4:"], #chart-key-body [data-series^="base:"]')]
        .map(b => b.dataset.series + '=' + b.textContent.trim())"""
    )
    fin = "sensor.santa_maria_hallway_current_temperature"
    check(
        f"{name}: v4 chips only for the sensors v4 has data for",
        sorted(v4_chips)
        == sorted(
            [
                "v4:_ca=Climate Advisor reading",
                "base:_ca=Climate Advisor reading",
                f"v4:{fin}=Santa Maria thermostat My ecobee Current Temperature (history only)",
                f"base:{fin}=Santa Maria thermostat My ecobee Current Temperature (history only)",
            ]
        ),
        str(v4_chips),
    )
    learning = pg.locator("#chart-v4-learning")
    check(
        f"{name}: one muted line names the sensors v4 is still learning",
        learning.count() == 1 and learning.inner_text().strip() == "v4 is still learning: thermostat, bedroom sensor",
        learning.inner_text() if learning.count() else "(none)",
    )
    check(f"{name}: the finer sensor's no-plan chip is disabled (no forecast)", chip(pg, f"base:{fin}").is_disabled())


with sync_playwright() as p:
    b = p.chromium.launch()
    for page in ("index.html", "new_dashboard.html"):
        for which in ("live", "full"):
            for view in ("24h", "6h", "history"):
                name = f"{page}:{which}:{view}"
                ctx, pg = open_page(b, page, which, name)
                if view == "6h":
                    pg.evaluate("() => setChartRange('6h')")
                    pg.wait_for_timeout(800)
                elif view == "history":
                    for _ in range(4):  # back two full views: the (shifted-to-now) payload is out of view
                        pg.evaluate("() => shiftChartWindow(-1)")
                        pg.wait_for_timeout(500)
                if which == "live" and view != "history":
                    live_shape(pg, name)
                n = audit(pg, name)
                if view != "history":
                    check(f"{name}: some chips are enabled", n >= 5, str(n))
                msg = pg.locator("#chart-lead-help")
                if view == "history" and msg.count():
                    txt = msg.inner_text()
                    check(f"{name}: scrolled history never says 'none yet'", "yet" not in txt, txt)
                pg.locator("#chart-key").screenshot(path=str(OUT / f"key-{page.split('.')[0]}-{which}-{view}.png"))
                pg.locator("#chart-stack").scroll_into_view_if_needed()
                pg.screenshot(path=str(OUT / f"chips-{page.split('.')[0]}-{which}-{view}.png"), full_page=True)
                ctx.close()
    b.close()

print(f"{checks - len(problems)}/{checks} checks passed")
print("\n".join(problems) or "no problems")
sys.exit(1 if problems else 0)
