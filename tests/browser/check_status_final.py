# ruff: noqa: E501
"""Real-browser check of the Status tab on BOTH dashboards (Issue #1230 R6fe). Not collected by pytest (no JS runtime in CI).

    pwvenv/bin/python tests/browser/check_status_final.py <repo-root> <screenshot-dir>

The classic page (index.html) and the new dashboard (new_dashboard.html) run the same chart module (chart_v2.js). For
1280 / 390 px x light / dark, on each page: no console errors or warnings, no horizontal scroll, the "Next 24 hours" list
(who does each step, "may help" on a low-confidence one, the 30-minute reminder, no mechanism words), the plan overlay chip
in the Overlays group (toggle, remembered across a reload), every Status control clicked once, and on the new page the text
contrast (>= 4.5:1, 3:1 for large text), the live night plan (with and without figures, empty), the readings and the
briefing. Screenshots: final-<page>-<scheme>-<width>.png.
"""

from __future__ import annotations

import datetime as dt
import functools
import http.server
import json
import math
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
CH = json.loads((ROOT / "tests/fixtures/chart_v2_zone1_24h.json").read_text())
ISO = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}")


class Quiet(http.server.ThreadingHTTPServer):
    def handle_error(self, *a):
        pass


http.server.SimpleHTTPRequestHandler.log_message = lambda *a, **k: None  # type: ignore[method-assign]
handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(FE))
srv = Quiet(("127.0.0.1", 0), handler)
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


def now_local() -> dt.datetime:
    return dt.datetime.now(dt.UTC).astimezone()


def timeline() -> dict:
    now = now_local()

    def at(m):
        return (now + dt.timedelta(minutes=m)).isoformat()

    def clock(m):
        # operation_timeline._fmt_time: the canonical sentence opens with the step's time ("Sun 6:15 AM" on another day)
        when = now + dt.timedelta(minutes=m)
        t = when.strftime("%I:%M %p").lstrip("0")
        return t if when.date() == now.date() else f"{when.strftime('%a')} {t}"

    def st(m, actor, kind, text, **kw):
        text = text.replace("{t}", clock(m)).replace("{wake}", clock(1700))
        if "text_short" in kw:
            kw["text_short"] = kw["text_short"].replace("{wake}", clock(1700))
        d = {
            "ts": at(m),
            "actor": actor,
            "kind": kind,
            "text": text,
            "reason": "t." + kind,
            "target_f": None,
            "mode": None,
            "confidence": "ok",
            "lead_minutes": 30 if actor == "you" else 0,
            "source": "t",
            "meta": {"id": f"{kind}{m}", "status": "planned", "facts": {}},
        }
        d.update(kw)
        return d

    return {
        "schema_version": 1,
        "zone": "z",
        "generated_at": at(-4),
        "day": now.date().isoformat(),
        "horizon_end": at(2880),
        "latched": True,
        "regen_reason": None,
        "unit": "fahrenheit",
        "steps": [
            st(
                -90,
                "ca",
                "setback_end",
                "A step that is already done.",
                meta={"id": "p", "status": "done", "facts": {}},
            ),
            # canonical `text` opens with the time; `text_short` is timeline_surfaces.plan_list_text (Issue #1399)
            st(
                45,
                "you",
                "windows_open",
                "{t}: open the windows. Outdoor is 6°F cooler than the house.",
                text_short="Open the windows. Outdoor is 6°F cooler than the house.",
            ),
            st(
                150,
                "ca",
                "precool_start",
                "{t}: CA pre-cools the house to 70°F so tomorrow's heat starts from a cooler house.",
                text_short="Pre-cools the house to 70°F so tomorrow's heat starts from a cooler house.",
            ),
            st(
                240,
                "you",
                "windows_close",
                "{t}: close the windows. Outdoor stops helping (it is warming past the house).",
                confidence="low",
                text_short="Close the windows. Outdoor stops helping (it is warming past the house).",
            ),
            st(300, "ca", "briefing_send", "{t}: CA sends the daily briefing.", text_short="Sends the daily briefing."),
            st(
                600,
                "ca",
                "sleep_band",
                "{t}: bedtime, CA switches to the sleep band, 66°F to 72°F, and reads the bedroom sensor until {wake}.",
                text_short="Bedtime: switches to the sleep band, 66°F to 72°F, and reads the bedroom sensor until {wake}.",
            ),
            st(
                1700,
                "ca",
                "wake_band",
                "{t}: CA switches to the daytime comfort band, 68°F to 74°F, and reads the thermostat again.",
                text_short="Switches to the daytime comfort band, 68°F to 74°F, and reads the thermostat again.",
            ),
        ],
        "path": [
            {"ts": at(i * 30), "temp_f": round(70 + 2.5 * math.sin(i / 5), 2), "sensor": "_ca", "model": "v3"}
            for i in range(60)
        ],
    }


def _night(show_numbers=True) -> dict:
    now = now_local()

    def at(m):
        return (now + dt.timedelta(minutes=m)).isoformat()

    def opt(key, label, base, amp, actions, **kw):
        d = {
            "key": key,
            "label": label,
            "class": "windows",
            "bedroom_low_f": base - amp,
            "bedroom_high_f": base + amp,
            "bedroom_wake_f": base - 1,
            "discomfort_fh": 0.0,
            "heat_min": 20,
            "cool_min": 0,
            "hvac_minutes": 20,
            "score": 0.5,
            "confidence": "high",
            "n_actions": len(actions),
            "runs": True,
            "note": "",
            "actions": actions,
            "path": [[at(30 + i * 15), round(base + amp * math.sin(i / 6), 2)] for i in range(33)],
        }
        d.update(kw)
        return d

    wo = {"ts": at(90), "kind": "windows_open", "actor": "you", "predicted": False, "through_night": False}
    wc = {"ts": at(210), "kind": "windows_close", "actor": "you", "predicted": False, "through_night": False}
    return {
        "schema_version": 1,
        "unit": "fahrenheit",
        "start": at(30),
        "end": at(510),
        "floor_f": 66,
        "ceiling_f": 72,
        "target_sensor": "sensor.bed",
        "fan_mode": "whole_house_fan",
        "recommended": "closed",
        "show_numbers": show_numbers,
        "options": [
            opt("closed", "Keep closed", 67, 1.5, []),
            opt("windows_2h", "Windows open 2 h", 66.5, 2, [wo, wc], score=0.9, heat_min=30, confidence="medium"),
        ],
    }


def night(show_numbers=True) -> dict:
    """As the endpoint answers: below medium confidence every figure is null and the path empty (never 0)."""
    d = _night(show_numbers)
    d["source_model"] = "v4"
    d["source_note"] = (
        "From v4. Tonight's bedroom plan is always built on v4's learned rates, whichever prediction model the Status chart shows."
    )
    d["headline"] = "Keep the windows closed tonight; CA keeps the heat for the bedroom at 66°F."
    d["tradeoffs"] = (
        [{"key": "windows_2h", "label": "Windows open 2 h", "text": "Windows open 2 h: 10 more minutes of heat or AC"}]
        if show_numbers
        else []
    )
    if not show_numbers:
        for o in d["options"]:
            for k in (
                "bedroom_low_f",
                "bedroom_high_f",
                "bedroom_wake_f",
                "discomfort_fh",
                "heat_min",
                "cool_min",
                "hvac_minutes",
                "score",
            ):
                o[k] = None
            o["path"] = []
    return d


CONTRAST_JS = """
() => {
  function parse(c){const m=c.match(/rgba?\\(([^)]+)\\)/);if(!m)return null;const p=m[1].split(',').map(s=>parseFloat(s));return {r:p[0],g:p[1],b:p[2],a:p.length>3?p[3]:1};}
  function lum(c){const f=v=>{v/=255;return v<=0.03928?v/12.92:Math.pow((v+0.055)/1.055,2.4)};return 0.2126*f(c.r)+0.7152*f(c.g)+0.0722*f(c.b);}
  function over(top,bot){const a=top.a;return {r:top.r*a+bot.r*(1-a),g:top.g*a+bot.g*(1-a),b:top.b*a+bot.b*(1-a),a:1};}
  function bgOf(el){const stack=[];for(let e=el;e;e=e.parentElement){const c=parse(getComputedStyle(e).backgroundColor);if(c&&c.a>0){stack.push(c);if(c.a>=1)break;}}
    let base={r:255,g:255,b:255,a:1};const body=parse(getComputedStyle(document.body).backgroundColor);if(body&&body.a>0)base=body;
    for(let i=stack.length-1;i>=0;i--)base=over(stack[i],base);return base;}
  const bad=[],seen=new Set();
  const w=document.createTreeWalker(document.querySelector('#panel-status'),NodeFilter.SHOW_TEXT);
  let n;while(n=w.nextNode()){
    if(!n.nodeValue.trim())continue;const el=n.parentElement;if(!el||seen.has(el))continue;seen.add(el);
    const cs=getComputedStyle(el);if(cs.visibility==='hidden'||cs.display==='none')continue;
    const r=el.getBoundingClientRect();if(r.width===0||r.height===0)continue;
    if(el.closest('[hidden],script,style,option'))continue;
    let op=1;for(let e=el;e;e=e.parentElement){op*=parseFloat(getComputedStyle(e).opacity);}
    const fg=parse(cs.color);if(!fg)continue;const bg=bgOf(el);const f=over({r:fg.r,g:fg.g,b:fg.b,a:fg.a*op},bg);
    const L1=lum(f),L2=lum(bg);const ratio=(Math.max(L1,L2)+0.05)/(Math.min(L1,L2)+0.05);
    const px=parseFloat(cs.fontSize),bold=parseInt(cs.fontWeight)>=700;const large=px>=24||(px>=18.66&&bold);
    if(ratio<(large?3:4.5)){bad.push(el.tagName.toLowerCase()+(el.id?'#'+el.id:'')+' "'+n.nodeValue.trim().slice(0,30)+'" '+ratio.toFixed(2));}
  }
  return bad;
}
"""

requests: list[tuple[str, str, str]] = []


def new_page(browser, vp, scheme, overrides=None, name=""):
    ctx = browser.new_context(viewport=vp, color_scheme=scheme, accept_downloads=True)
    ctx.add_init_script(
        "localStorage.setItem('hassTokens', JSON.stringify({access_token:'t',token_type:'Bearer',expires:Date.now()+3.6e6}))"
    )
    pg = ctx.new_page()
    expected_failure = name.startswith(("night:", "plan:"))  # those cases answer 404/500/503 on purpose

    def on_console(m):
        if m.type not in ("error", "warning"):
            return
        if expected_failure and m.text.startswith("Failed to load resource"):
            return
        problems.append(f"{name} console.{m.type}: {m.text}")

    pg.on("console", on_console)
    pg.on("pageerror", lambda e: problems.append(f"{name} pageerror: {e}"))
    overrides = overrides or {}

    def route(r):
        u = urllib.parse.urlparse(r.request.url)
        ep = u.path.split("/api/climate_advisor/")[-1]
        requests.append((r.request.method, ep, r.request.post_data or ""))

        def j(body, code=200):
            r.fulfill(status=code, content_type="application/json", body=json.dumps(body))

        if ep in overrides:
            o = overrides[ep]
            return j(*(o if isinstance(o, tuple) else (o,)))
        if r.request.method == "POST":
            return j({"message": "ok", "success": True})
        if ep == "status":
            st = dict(
                FX["status"],
                zone_count=1,
                zones=[],
                manual_override_active=False,
                grace_active=False,
                nat_vent_active=True,
                nat_vent_ac_assist=False,
                nat_vent_off_threshold=66,
                nat_vent_on_threshold=72,
            )
            return j(st)
        if ep == "chart_data":
            delta = now_local() - dt.datetime.fromisoformat(CH["now"])
            return j(shift(CH["chart_data"], delta))
        if ep == "timeline":
            return j(timeline())
        if ep == "night_strategy":
            return j(night())
        if ep == "briefing":
            return j({"briefing": "Today's steps:\n  - Open windows when the plan says so."})
        if ep in FX:
            return j(FX[ep])
        return j({})

    pg.route("**/api/climate_advisor/**", route)
    return ctx, pg


def layout(pg, name, vp):
    sw = pg.evaluate("document.documentElement.scrollWidth")
    check(f"{name}: no horizontal scroll", sw <= vp["width"] + 1, f"{sw}px > {vp['width']}px")


READOUT_JS = """() => {
  const r = id => document.getElementById(id).getBoundingClientRect();
  const p = document.getElementById('chart-hover-panel');
  const t = p.querySelector('.chp-time');
  const names = [...p.querySelectorAll('.chp-grid .chp-item')].map(e => e.textContent.split(':')[0].trim());
  return { stackBottom: r('chart-stack').bottom, top: r('chart-hover-panel').top, h: p.offsetHeight,
           chartTop: r('temp-chart').top + window.scrollY, html: p.innerHTML, mode: p.dataset.mode || '',
           time: t ? t.textContent : '', hint: !!p.querySelector('.chp-hint'), names: names };
}"""

NOW_PX_JS = """() => { const c = Chart.getChart(document.getElementById('temp-chart')); return c.scales.x.getPixelForValue(Date.now()); }"""


def _shot(pg, name, label, kind, h_readout):
    y = pg.evaluate("window.scrollY") + pg.locator("#chart-stack").bounding_box()["y"]
    h = pg.locator("#chart-stack").bounding_box()["height"]
    pg.screenshot(
        path=str(OUT / f"{kind}-{name.split('/')[0]}-{label}.png"),
        full_page=True,
        clip={"x": 0, "y": max(0, y - 60), "width": pg.viewport_size["width"], "height": h + 60 + h_readout + 40},
    )


def readout_checks(pg, name, label):
    # Issue #1408: the readout sits below the chart and, with the pointer off the chart, shows what a hover at NOW
    # would (labelled "Now · <time>", with a small hover/pin hint), never a blank line.
    rest = pg.evaluate(READOUT_JS)
    check(f"{name}: readout is below the chart", rest["top"] >= rest["stackBottom"] - 0.5, str(rest)[:200])
    check(
        f"{name}: readout rests on NOW", rest["mode"] == "rest" and rest["time"].startswith("Now \u00b7 "), rest["time"]
    )
    check(f"{name}: NOW readout has rows", "chp-row" in rest["html"] and "Indoor" in rest["html"], rest["html"][:200])
    check(f"{name}: NOW readout keeps the hover/pin hint", rest["hint"], rest["time"])
    _shot(pg, name, label, "readout-now", rest["h"])
    # the same rows as a hover at NOW (one row builder)
    pg.evaluate(f"() => window.__triggerHoverAt(({NOW_PX_JS})())")
    pg.wait_for_timeout(100)
    at_now = pg.evaluate(READOUT_JS)
    check(
        f"{name}: NOW readout = hover at NOW", at_now["names"] == rest["names"], f"{rest['names']} vs {at_now['names']}"
    )
    pg.evaluate(
        "() => window.__triggerHoverAt(document.getElementById('temp-chart').getBoundingClientRect().width * 0.4)"
    )
    pg.wait_for_timeout(150)
    hov = pg.evaluate(READOUT_JS)
    check(f"{name}: hover fills the readout", "chp-row" in hov["html"] and hov["mode"] == "hover", f"{hov['h']}px")
    check(f"{name}: hover does not shrink the readout", hov["h"] >= rest["h"], f"{hov['h']} < {rest['h']}px")
    check(f"{name}: hover does not move the chart", abs(hov["chartTop"] - rest["chartTop"]) < 0.5)
    _shot(pg, name, label, "readout", hov["h"])
    pg.evaluate("() => document.getElementById('chart-stack').dispatchEvent(new MouseEvent('mouseleave'))")
    pg.wait_for_timeout(100)
    back = pg.evaluate(READOUT_JS)
    check(
        f"{name}: leaving the chart returns to NOW",
        back["mode"] == "rest" and back["time"].startswith("Now \u00b7 "),
        back["time"],
    )
    check(f"{name}: leaving keeps the readout height", back["h"] == hov["h"], f"{back['h']} vs {hov['h']}px")


def click_every_status_control(pg, name):
    """Every visible Status button once (re-queried as the DOM redraws). Navigation links are skipped."""
    open_key(pg)
    seen, root = set(), "#panel-status, #tab-status"
    for _ in range(80):
        btns = pg.locator(f":is({root}) button:visible")
        target = None
        for i in range(btns.count()):
            b = btns.nth(i)
            try:
                key = (
                    (b.get_attribute("id") or "")
                    + "|"
                    + b.inner_text(timeout=400)[:24]
                    + "|"
                    + (b.get_attribute("data-series") or "")
                    + (b.get_attribute("data-range") or "")
                    + (b.get_attribute("data-ahead") or "")
                    + (b.get_attribute("data-lead") or "")
                    + (b.get_attribute("data-opt") or "")
                    + (b.get_attribute("data-night") or "")
                )
            except Exception:
                continue
            if key in seen:
                continue
            seen.add(key)
            if b.is_disabled():
                continue
            target = b
            break
        if target is None:
            break
        try:
            with pg.expect_download(timeout=250):
                target.click(timeout=1500)
        except Exception:
            try:
                target.click(timeout=1500)
            except Exception as e:
                if "Timeout" not in str(e):
                    problems.append(f"{name}: click failed {str(e)[:80]}")
        pg.wait_for_timeout(60)
        if pg.locator("dialog[open]").count():
            pg.keyboard.press("Escape")
    return len(seen)


def open_key(pg):
    """On a phone the key is folded into a 'Lines' sheet: open it (as an occupant would) before using a chip."""
    if (
        pg.evaluate("document.getElementById('chart-key').open") is False
        and pg.locator("#chart-key > summary").is_visible()
    ):
        pg.locator("#chart-key > summary").click()
        pg.wait_for_timeout(100)


def plan_checks(pg, name, list_sel):
    rows = pg.locator(f"{list_sel} .plan-row")
    check(
        f"{name}: plan list has the four steps that matter (done, briefing and beyond-24h steps are left out)",
        rows.count() == 4,
        str(rows.count()),
    )
    kinds = rows.evaluate_all("els => els.map(e => e.dataset.kind)")
    check(
        f"{name}: steps in time order",
        kinds == ["windows_open", "precool_start", "windows_close", "sleep_band"],
        str(kinds),
    )
    check(
        f"{name}: 'you' and 'CA' are named",
        "you" in rows.nth(0).inner_text().lower() and "ca" in rows.nth(1).inner_text().lower(),
    )
    check(
        f"{name}: low-confidence step says 'may help'",
        pg.locator(f'{list_sel} .plan-row[data-kind="windows_close"] .plan-qual').inner_text() == "may help",
    )
    check(
        f"{name}: 'you' step shows the 30-minute reminder",
        "Reminder at" in pg.locator(f'{list_sel} .plan-row[data-kind="windows_open"]').inner_text(),
    )
    check(
        f"{name}: no reminder on a CA step",
        "Reminder" not in pg.locator(f'{list_sel} .plan-row[data-kind="precool_start"]').inner_text(),
    )
    # Issue #1399: the row's sentence does not repeat the time its own time column shows, starts with a capital, and a
    # CA row does not open with "CA" beside the CA badge
    cells = rows.evaluate_all(
        "els => els.map(e => ({actor: e.dataset.actor, time: e.querySelector('.plan-time').innerText.trim(),"
        " text: e.querySelector('.plan-text').innerText.trim()}))"
    )
    for c in cells:
        clock = next((w for w in c["time"].split() if any(ch.isdigit() for ch in w)), c["time"])
        check(
            f"{name}: row text does not open with its own time",
            not c["text"].startswith((c["time"], clock))
            and not re.match(r"^(?:[A-Za-z]{2,4}\.? )?\d{1,2}:\d{2}", c["text"]),
            f"{c['time']} | {c['text'][:60]}",
        )
        check(f"{name}: row text starts with a capital", c["text"][:1].isupper(), c["text"][:60])
        if c["actor"] == "ca":
            check(f"{name}: CA row text does not open with 'CA'", not re.match(r"^CA\b", c["text"]), c["text"][:60])
    bed = pg.locator(f'{list_sel} .plan-row[data-kind="sleep_band"] .plan-text').inner_text()
    check(
        f"{name}: bedtime row reads 'Bedtime: switches to the sleep band, ...'",
        bed.startswith("Bedtime: switches to the sleep band, 66°F to 72°F"),
        bed[:80],
    )
    text = pg.locator(list_sel).inner_text().lower()
    for word in ("waiting", "paused", "grace", "override"):
        check(f"{name}: plan list has no mechanism word {word!r}", word not in text)
    open_key(pg)
    chip = pg.locator('#chart-key-body .key-group[aria-label="Overlays"] [data-series="ov:plan"]')
    check(f"{name}: plan chip is in the Overlays group", chip.count() == 1)
    check(f"{name}: plan overlay starts on", chip.get_attribute("aria-pressed") == "true")
    n = pg.evaluate(
        "Chart.getChart(document.getElementById('temp-chart')).data.datasets.filter(d => d.caId === 'ov:plan').length"
    )
    check(f"{name}: plan path is a chart dataset", n == 1, str(n))
    chip.click()
    pg.wait_for_timeout(120)
    hidden = pg.evaluate(
        "Chart.getChart(document.getElementById('temp-chart')).data.datasets.filter(d => d.caId === 'ov:plan')[0].hidden"
    )
    check(f"{name}: toggling hides the overlay", hidden is True)
    pg.reload()
    pg.wait_for_timeout(1500)
    open_key(pg)
    check(
        f"{name}: the choice is remembered after a reload",
        pg.locator('#chart-key-body [data-series="ov:plan"]').get_attribute("aria-pressed") == "false",
    )
    check(f"{name}: the list stays when the overlay is off", pg.locator(f"{list_sel} .plan-row").count() == 4)
    pg.locator('#chart-key-body [data-series="ov:plan"]').click()
    pg.wait_for_timeout(100)


def run_page(browser, page, label, vp, scheme):
    name = f"{page.split('.')[0]}/{label}"
    new = page == "new_dashboard.html"
    ctx, pg = new_page(browser, vp, scheme, name=name)
    pg.goto(f"http://127.0.0.1:{PORT}/{page}")
    pg.wait_for_timeout(1800)
    if new:
        for sel in (
            "#status-for-you",
            "#status-grid",
            "#status-chart",
            "#status-plan-list",
            "#night-card",
            "#status-briefing",
        ):
            check(f"{name}: {sel} is on the page", pg.locator(sel).count() == 1)
        txt = pg.locator("#status-grid").inner_text()
        for needle in (
            "Hallway",
            "Bedroom",
            "Outdoor sensor",
            "service says",
            "Today",
            "Doors & windows",
            "Whole-house fan",
            "comfort score",
        ):
            check(f"{name}: readings show {needle!r}", needle.lower() in txt.lower(), txt[:200])
        check(
            f"{name}: nat-vent cycling is on the plan list",
            "cycling 66°F–72°F" in pg.locator("#status-natvent-line").inner_text(),
        )
        check(
            f"{name}: For you / CA next",
            bool(pg.locator("#status-for-you").inner_text().strip())
            and bool(pg.locator("#status-ca-next-time").inner_text().strip()),
        )
    plan_checks(pg, name, "#status-plan-list" if new else "#plan-list")
    layout(pg, name, vp)
    readout_checks(pg, name, label)
    if new:
        bad = pg.evaluate(CONTRAST_JS)
        check(f"{name}: Status text contrast", not bad, f"{len(bad)} low: {bad[:5]}")
    pg.screenshot(path=str(OUT / f"final-{page.split('.')[0]}-{label}.png"), full_page=True)
    if scheme == "light":
        n = click_every_status_control(pg, name)
        check(f"{name}: clicked {n} Status controls", n >= 20, str(n))
        layout(pg, f"{name}/after-clicks", vp)
        if new:
            bad = pg.evaluate(CONTRAST_JS)
            check(f"{name}/after-clicks: Status text contrast", not bad, f"{len(bad)} low: {bad[:5]}")
    ctx.close()


def new_status_behaviour(browser):
    # night plan: live, no figures, empty, missing endpoint
    for label, ov, expect in [
        ("live", {}, "live"),
        ("nonumbers", {"night_strategy": night(False)}, "nonumbers"),
        ("empty", {"night_strategy": ({"error": "not_ready"}, 503)}, "empty"),
        ("missing", {"night_strategy": ({}, 404)}, "sample"),
    ]:
        ctx, pg = new_page(browser, {"width": 1280, "height": 900}, "light", ov, name=f"night:{label}")
        pg.goto(f"http://127.0.0.1:{PORT}/new_dashboard.html")
        pg.wait_for_timeout(1500)
        badge = pg.locator("#night-sample-badge").is_visible()
        svg = pg.locator("#night-card svg[role=img]").count()
        txt = pg.locator("#night-card").inner_text()
        if pg.locator("#night-headline").count():  # the headline names the sleep band (a setting), not a prediction
            txt = txt.replace(pg.locator("#night-headline").inner_text(), "")
        if expect == "live":
            check(
                "night live: chart, table, no sample badge, steps",
                svg == 1
                and pg.locator("#night-card table").count() == 1
                and not badge
                and pg.locator("#night-steps").count() == 1,
            )
            pg.locator("#night-card [data-opt]").first.click()
            pg.wait_for_timeout(100)
            check(
                "night live: an option toggles off",
                pg.locator("#night-card [data-opt]").first.get_attribute("aria-pressed") == "false",
            )
        elif expect == "nonumbers":
            check(
                "night without figures: no chart, no table, no temperatures, says why",
                svg == 0 and "°" not in txt and pg.locator("#night-nonumbers").count() == 1 and "Keep closed" in txt,
                txt[:160],
            )
            pg.screenshot(path=str(OUT / "final-new_dashboard-night-nonumbers.png"), full_page=False)
        elif expect == "empty":
            check("night empty: plain empty state, no badge", "No night plan yet" in txt and not badge, txt[:120])
        else:
            check("night 404: the labelled sample", badge and pg.locator("#night-card [data-opt]").count() > 1)
        ctx.close()
    # plan list states
    for label, ov, expect in [
        ("503", {"timeline": ({"error": "timeline_not_ready"}, 503)}, "after Climate Advisor has classified the day"),
        ("500", {"timeline": ({"error": "boom"}, 500)}, "could not be loaded"),
        ("empty", {"timeline": {**timeline(), "steps": [], "path": []}}, "Nothing is planned"),
    ]:
        ctx, pg = new_page(browser, {"width": 1280, "height": 900}, "light", ov, name=f"plan:{label}")
        pg.goto(f"http://127.0.0.1:{PORT}/new_dashboard.html")
        pg.wait_for_timeout(1500)
        check(
            f"plan {label}: empty state text",
            expect in pg.locator("#status-plan-list").inner_text(),
            pg.locator("#status-plan-list").inner_text()[:100],
        )
        ctx.close()
    # briefing: collapsed, text, Regenerate (notify false), Send
    ctx, pg = new_page(browser, {"width": 1280, "height": 900}, "light", name="briefing")
    pg.goto(f"http://127.0.0.1:{PORT}/new_dashboard.html")
    pg.wait_for_timeout(1200)
    check("briefing: collapsed", pg.evaluate("document.getElementById('status-briefing').open") is False)
    pg.locator("#status-briefing > summary").click()
    check("briefing: shows the text", "Today's steps" in pg.locator("#status-briefing-text").inner_text())
    before = len(requests)
    pg.locator("#status-briefing-regenerate").click()
    pg.wait_for_timeout(300)
    pg.locator("#status-briefing-send").click()
    pg.wait_for_timeout(300)
    posts = [(e, b) for m, e, b in requests[before:] if m == "POST" and e.startswith("send_briefing")]
    check(
        "briefing: Regenerate posts {notify:false}, Send posts no body",
        len(posts) == 2 and json.loads(posts[0][1]) == {"notify": False} and posts[1][1] == "",
        str(posts),
    )
    pg.screenshot(path=str(OUT / "final-new_dashboard-briefing-open.png"), full_page=False)
    ctx.close()
    # For you / CA next come from the status payload
    st = dict(
        FX["status"],
        zone_count=1,
        zones=[],
        next_action="Open windows at 6:10 PM",
        next_automation_action="Start cooling early",
        next_automation_time="7:40 PM",
    )
    ctx, pg = new_page(browser, {"width": 1280, "height": 900}, "light", {"status": st}, name="next")
    pg.goto(f"http://127.0.0.1:{PORT}/new_dashboard.html")
    pg.wait_for_timeout(1200)
    check(
        "For you / CA next show the payload",
        pg.locator("#status-for-you").inner_text() == "Open windows at 6:10 PM"
        and pg.locator("#status-ca-next").inner_text() == "Start cooling early"
        and pg.locator("#status-ca-next-time").inner_text() == "7:40 PM",
    )
    ctx.close()


def classic_celsius(browser):
    ch = shift(CH["chart_data"], now_local() - dt.datetime.fromisoformat(CH["now"]))
    tl = timeline()
    tl["unit"] = "celsius"
    tl["path"] = [{**p, "temp_f": round((p["temp_f"] - 32) * 5 / 9, 2)} for p in tl["path"]]
    st = dict(FX["status"], zone_count=1, zones=[], unit="celsius")
    ctx, pg = new_page(
        browser,
        {"width": 1280, "height": 900},
        "light",
        {"status": st, "timeline": tl, "chart_data": {**ch, "unit": "celsius"}},
        name="classic-celsius",
    )
    pg.goto(f"http://127.0.0.1:{PORT}/index.html")
    pg.wait_for_timeout(1500)
    ys = pg.evaluate(
        "Chart.getChart(document.getElementById('temp-chart')).data.datasets.filter(d => d.caId === 'ov:plan')[0].data.map(p => p.y)"
    )
    check(
        "classic celsius: the plan path is drawn in the payload's unit",
        ys and max(ys) < 30,
        str(max(ys) if ys else None),
    )
    ctx.close()


def v4_ca_only(browser):
    """Issue #1400: the live 0.9.4 model is "_ca"-only. Both pages draw v4's "Climate Advisor reading" line, broken at
    every bedtime / wake-up swap (no segment crosses one). Screenshots: final-<page>-v4-ca-<scheme>.png."""
    ch = shift(CH["chart_data"], now_local() - dt.datetime.fromisoformat(CH["now"]))
    v4 = ch["v4"]
    hall = v4["sensors"][0]
    swaps = []
    now = now_local()
    for day in (-1, 0, 1):
        d = (now + dt.timedelta(days=day)).date()
        for hh, mm in ((22, 30), (7, 0)):
            t = dt.datetime(d.year, d.month, d.day, hh, mm, tzinfo=now.tzinfo)
            if now - dt.timedelta(hours=24) < t < now + dt.timedelta(hours=24):
                swaps.append(t.isoformat())
    ca = {
        **v4,
        "sensors": ["_ca"],
        "past": {"_ca": v4["past"][hall]},
        "future": {"_ca": v4["future"][hall]},
        "baseline": {"_ca": v4["baseline"][hall]},
        "breaks": {"_ca": sorted(swaps)},
    }
    engines = {
        "primary": "v4", "shadow": "none", "fallback": False, "fitting": False, "primary_state": "ready", "text": "",
        "engines": [
            {"id": "v3", "label": "v3", "running": False, "primary": False, "shadow": False, "usable": True},
            {"id": "v4", "label": "v4", "running": True, "primary": True, "shadow": False, "usable": True},
        ],
    }  # fmt: skip
    for page in ("index.html", "new_dashboard.html"):
        for scheme in ("light", "dark"):
            name = f"v4-ca:{page}:{scheme}"
            ctx, pg = new_page(
                browser,
                {"width": 1280, "height": 1000},
                scheme,
                {"chart_data": {**ch, "v4": ca, "prediction_engines": engines}},
                name=name,
            )
            pg.goto(f"http://127.0.0.1:{PORT}/{page}")
            pg.wait_for_timeout(1500)
            got = pg.evaluate(
                """(swaps) => { const c = Chart.getChart(document.getElementById('temp-chart'));
                const d = c && c.data.datasets.find(x => x.caId === 'v4:_ca'); if (!d) return null;
                const pts = d.data, crossed = swaps.map(Date.parse).filter(s => {
                  const i = pts.findIndex(p => p.x >= s); if (i <= 0) return false;
                  let j = i - 1; while (j >= 0 && pts[j].y == null) j--; return j === i - 1; });
                return { hidden: !!d.hidden, n: pts.filter(p => p.y != null).length, crossed: crossed.length }; }""",
                swaps,
            )
            check(f"{name}: v4 _ca line drawn", bool(got) and not got["hidden"] and got["n"] > 10, str(got))
            check(f"{name}: no segment crosses a sensor swap", bool(got) and got["crossed"] == 0, str(got))
            open_key(pg)  # the key starts folded since #1399
            check(
                f"{name}: key names the line", "Climate Advisor reading" in pg.locator("#chart-key-body").inner_text()
            )
            pg.locator("#temp-chart").scroll_into_view_if_needed()
            pg.screenshot(path=str(OUT / f"final-{page.split('.')[0]}-v4-ca-{scheme}.png"), full_page=False)
            ctx.close()


BANK_SENTENCE = "Pre-cooling to 77°F until the 4:15 PM high-cost period, then back to the Away band 60–80°F"


def tou_bank(browser):
    """Issue #1404: Away, pre-cooling to 77 F for a 4:15 PM high-cost period. Both pages, light / dark, 1280 / 390 px:
    the Status line says the bank sentence, the hallway card's setpoint agrees with what CA wants (no "wants 80"), the
    plan list opens with the same sentence ("Now"), nothing scrolls sideways. Screenshots final-<page>-tou-bank-*.png."""
    ends = (now_local() + dt.timedelta(hours=2)).isoformat()
    hold = {
        "kind": "tou_bank", "mode": "cool", "target": 77, "ends": ends, "schedule_id": "peak1",
        "heat": 60, "cool": 77, "schedule_heat": 60, "schedule_cool": 80, "after_heat": 60, "after_cool": 80,
        "text": BANK_SENTENCE, "after_text": "back to the Away band 60–80°F",
    }  # fmt: skip
    line = BANK_SENTENCE[0].lower() + BANK_SENTENCE[1:]
    st = dict(
        FX["status"], zone_count=1, zones=[], manual_override_active=False, grace_active=False, nat_vent_active=False,
        hvac_mode="cool", hvac_action="cooling", current_setpoint=77, ca_target_heat=60, ca_target_cool=77,
        occupancy_mode="away", automation_status=line, next_action=f"Nothing needs you: {line}.",
        next_automation_action="High-cost period starts — back to the Away band 60–80°F", next_automation_time="4:15 PM",
        current_hold=hold,
    )  # fmt: skip
    tl = dict(timeline(), current_hold=hold)
    for page, plan in (("index.html", "#plan-list"), ("new_dashboard.html", "#status-plan-list")):
        for label, vp, scheme in (
            ("light-1280", {"width": 1280, "height": 1000}, "light"),
            ("dark-390", {"width": 390, "height": 900}, "dark"),
        ):
            name = f"tou-bank:{page}:{label}"
            ctx, pg = new_page(browser, vp, scheme, {"status": st, "timeline": tl}, name=name)
            pg.goto(f"http://127.0.0.1:{PORT}/{page}")
            pg.wait_for_timeout(1500)
            grid = pg.locator("#status-grid").inner_text()
            check(f"{name}: Status says the bank sentence", BANK_SENTENCE.lower() in grid.lower(), grid[:200])
            check(f"{name}: no 'Climate Advisor wants' against the bank target", "wants" not in grid, grid[:200])
            first = pg.locator(f"{plan} .plan-row").first
            check(
                f"{name}: the plan list opens with the bank (Now)",
                first.count() == 1
                and first.get_attribute("data-kind") == "tou_bank"
                and first.locator(".plan-text").inner_text() == BANK_SENTENCE,
            )
            layout(pg, name, vp)
            pg.screenshot(path=str(OUT / f"final-{page.split('.')[0]}-tou-bank-{label}.png"), full_page=True)
            ctx.close()


def tou_bank_history(browser):
    """Issue #1406: the past band draws a time-of-use bank as what the thermostat held. Rows of the last 4 h carry
    hold_lower/hold_upper 60/77 (an Away cool bank to 77 F, band schedule 77-80 F); older rows have no hold (written
    before 0.9.6) and keep 77-80. Both pages, light 1280 / dark 390. Screenshots final-<page>-tou-history-*.png."""
    ch = shift(CH["chart_data"], now_local() - dt.datetime.fromisoformat(CH["now"]))
    now = now_local()
    rows = []
    for e in ch["state_log"]:
        t = dt.datetime.fromisoformat(e["ts"])
        if t > now:
            continue
        e = {**e, "lower": 77.0, "upper": 80.0}
        if now - t < dt.timedelta(hours=4):
            e.update(hold_lower=60.0, hold_upper=77.0)
        else:
            e.pop("hold_lower", None)
            e.pop("hold_upper", None)
        rows.append(e)
    ch = {**ch, "state_log": rows}
    probe = """(ago) => { const c = Chart.getChart(document.getElementById('temp-chart')); if (!c) return null;
        const up = c.data.datasets.find(d => d.label === 'Target Band'), lo = c.data.datasets.find(d => d.label === '_target_band_lower');
        if (!up || !lo || !up.data.length) return null; const t = Date.now() - ago * 3600000;
        const near = ds => ds.data.reduce((b, p) => (Math.abs(p.x - t) < Math.abs(b.x - t) ? p : b)).y;
        return [near(lo), near(up)]; }"""
    for page in ("index.html", "new_dashboard.html"):
        for label, vp, scheme in (
            ("light-1280", {"width": 1280, "height": 1000}, "light"),
            ("dark-390", {"width": 390, "height": 900}, "dark"),
        ):
            name = f"tou-history:{page}:{label}"
            ctx, pg = new_page(browser, vp, scheme, {"chart_data": ch}, name=name)
            pg.goto(f"http://127.0.0.1:{PORT}/{page}")
            pg.wait_for_timeout(1500)
            held, old = pg.evaluate(probe, 2), pg.evaluate(probe, 6)
            check(f"{name}: a past bank row draws the hold 60-77", held == [60, 77], str(held))
            check(f"{name}: a pre-0.9.6 row keeps the schedule shape 77-80", old == [77, 80], str(old))
            layout(pg, name, vp)
            pg.locator("#temp-chart").scroll_into_view_if_needed()
            pg.screenshot(path=str(OUT / f"final-{page.split('.')[0]}-tou-history-{label}.png"), full_page=False)
            ctx.close()


with sync_playwright() as p:
    b = p.chromium.launch()
    tou_bank(b)
    tou_bank_history(b)
    v4_ca_only(b)
    for page in ("index.html", "new_dashboard.html"):
        for label, vp, scheme in [
            ("light-1280", {"width": 1280, "height": 1000}, "light"),
            ("dark-1280", {"width": 1280, "height": 1000}, "dark"),
            ("light-390", {"width": 390, "height": 900}, "light"),
            ("dark-390", {"width": 390, "height": 900}, "dark"),
        ]:
            run_page(b, page, label, vp, scheme)
    new_status_behaviour(b)
    classic_celsius(b)
    b.close()

print(f"{checks - len(problems)}/{checks} checks passed")
print("\n".join(problems) or "no problems")
sys.exit(1 if problems else 0)
