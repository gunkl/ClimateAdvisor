# ruff: noqa: E501
"""Real-browser check of the new dashboard (Issue #1230 R5). Not collected by pytest (no JS runtime in CI).

    pwvenv/bin/python tests/browser/check_new_dashboard.py <repo-root> <screenshot-dir>

Serves the frontend folder, answers /api/** from tests/fixtures/new_dashboard_api.json (shapes captured from the
real endpoints), sets hassTokens, and for 1280/390 px x light/dark: opens every tab, clicks every visible button,
checks for console errors, horizontal scroll and text contrast (>= 4.5:1, 3:1 for large text). Also drives the
banners, deep links, keyboard model, zone switching and the "no new write path" rule.
"""

from __future__ import annotations

import datetime as dt
import functools
import http.server
import json
import math
import pathlib
import sys
import threading
import urllib.parse

from playwright.sync_api import sync_playwright

ROOT = pathlib.Path(sys.argv[1])
OUT = pathlib.Path(sys.argv[2])
OUT.mkdir(parents=True, exist_ok=True)
FE = ROOT / "custom_components/climate_advisor/frontend"
FX = json.loads((ROOT / "tests/fixtures/new_dashboard_api.json").read_text())
PA = (ROOT / "tests/fixtures/prediction_accuracy_sample.json").read_text()

handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(FE))
handler.log_message = lambda *a, **k: None  # type: ignore[attr-defined]
srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
PORT = srv.server_port
threading.Thread(target=srv.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{PORT}/new_dashboard.html"

problems: list[str] = []
requests: list[tuple[str, str, str, str]] = []  # method, endpoint, query, body

CONTRAST_JS = """
() => {
  function parse(c){const m=c.match(/rgba?\\(([^)]+)\\)/);if(!m)return null;const p=m[1].split(',').map(s=>parseFloat(s));return {r:p[0],g:p[1],b:p[2],a:p.length>3?p[3]:1};}
  function lum(c){const f=v=>{v/=255;return v<=0.03928?v/12.92:Math.pow((v+0.055)/1.055,2.4)};return 0.2126*f(c.r)+0.7152*f(c.g)+0.0722*f(c.b);}
  function over(top,bot){const a=top.a;return {r:top.r*a+bot.r*(1-a),g:top.g*a+bot.g*(1-a),b:top.b*a+bot.b*(1-a),a:1};}
  function bgOf(el){const stack=[];for(let e=el;e;e=e.parentElement){const c=parse(getComputedStyle(e).backgroundColor);if(c&&c.a>0){stack.push(c);if(c.a>=1)break;}}
    let base={r:255,g:255,b:255,a:1};const body=parse(getComputedStyle(document.body).backgroundColor);if(body&&body.a>0)base=body;
    for(let i=stack.length-1;i>=0;i--)base=over(stack[i],base);return base;}
  const bad=[],seen=new Set();
  const w=document.createTreeWalker(document.body,NodeFilter.SHOW_TEXT);
  let n;while(n=w.nextNode()){
    if(!n.nodeValue.trim())continue;const el=n.parentElement;if(!el||seen.has(el))continue;seen.add(el);
    const cs=getComputedStyle(el);if(cs.visibility==='hidden'||cs.display==='none')continue;
    const r=el.getBoundingClientRect();if(r.width===0||r.height===0)continue;
    if(el.closest('[hidden],script,style,option')||el.closest('iframe'))continue;
    let op=1;for(let e=el;e;e=e.parentElement){op*=parseFloat(getComputedStyle(e).opacity);}
    const fg=parse(cs.color);if(!fg)continue;const bg=bgOf(el);const f=over({r:fg.r,g:fg.g,b:fg.b,a:fg.a*op},bg);
    const L1=lum(f),L2=lum(bg);const ratio=(Math.max(L1,L2)+0.05)/(Math.min(L1,L2)+0.05);
    const px=parseFloat(cs.fontSize),bold=parseInt(cs.fontWeight)>=700;const large=px>=24||(px>=18.66&&bold);
    if(ratio<(large?3:4.5)){let sel=el.tagName.toLowerCase()+(el.id?'#'+el.id:'')+(el.className&&typeof el.className==='string'?'.'+el.className.split(' ').join('.'):'');
      bad.push(sel+' "'+n.nodeValue.trim().slice(0,30)+'" '+ratio.toFixed(2));}
  }
  return bad;
}
"""


def live_timeline() -> dict:
    """GET timeline as the endpoint returns it (R3b contract, display unit): steps and path relative to now."""
    now = dt.datetime.now(dt.UTC).astimezone()

    def at(m: int) -> str:
        return (now + dt.timedelta(minutes=m)).isoformat()

    def st(m, actor, kind, text, **kw):
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
            st(45, "you", "windows_open", "Open windows at the time shown. Outdoor air is cooler than the house."),
            st(150, "ca", "precool_start", "Start cooling a little early to ride out the afternoon."),
            st(240, "you", "windows_close", "Close windows before the evening cools off.", confidence="low"),
            st(600, "ca", "sleep_band", "Bedtime: comfort moves to the bedroom, 66 to 72."),
        ],
        "path": [
            {"ts": at(i * 30), "temp_f": round(70 + 2.5 * math.sin(i / 5), 2), "sensor": "_ca", "model": "v3"}
            for i in range(60)
        ],
    }


def _live_night(show_numbers: bool = True) -> dict:
    """GET night_strategy as NightStrategy.to_dict() (R4) with the display unit added."""
    now = dt.datetime.now(dt.UTC).astimezone()

    def at(m: int) -> str:
        return (now + dt.timedelta(minutes=m)).isoformat()

    def path(base, amp):
        return [[at(30 + i * 15), round(base + amp * math.sin(i / 6), 2)] for i in range(33)]

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
            "path": path(base, amp),
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


def live_night(show_numbers: bool = True) -> dict:
    """As the endpoint answers: with the v4 gate closed every figure is null and the path empty (never 0)."""
    d = _live_night(show_numbers)
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


def route_factory(overrides: dict):
    def route(r):
        u = urllib.parse.urlparse(r.request.url)
        ep = u.path.split("/api/climate_advisor/")[-1]
        body = r.request.post_data or ""
        requests.append((r.request.method, ep, u.query, body))
        if ep in overrides:
            payload = overrides[ep]
            if callable(payload):
                code, payload = payload()
                r.fulfill(status=code, content_type="application/json", body=json.dumps(payload))
                return
        elif ep == "zones":  # Issue #1454: the zone list, asked before any scoped call
            zs = FX["status"]["zones"]
            payload = {"zones": zs, "zone_count": len(zs)}
        elif ep == "prediction_accuracy":
            r.fulfill(status=200, content_type="application/json", body=PA)
            return
        elif r.request.method == "POST":
            payload = {"message": "ok", "success": True, "automation_enabled": False}
        elif ep == "chart_data":
            payload = FX["chart_data"]
        elif ep == "timeline":
            payload = live_timeline()
        elif ep == "night_strategy":
            payload = live_night()
        elif ep.startswith("activity_record"):
            payload = FX["activity_record"]
        elif ep in FX:
            payload = FX[ep]
        else:
            payload = {}
        r.fulfill(status=200, content_type="application/json", body=json.dumps(payload))

    return route


def new_page(browser, vp, scheme, overrides=None, name=""):
    ctx = browser.new_context(viewport=vp, color_scheme=scheme, accept_downloads=True)
    ctx.add_init_script(
        "localStorage.setItem('hassTokens', JSON.stringify({access_token:'t',token_type:'Bearer',expires:Date.now()+3.6e6}))"
    )
    pg = ctx.new_page()
    restart = name.startswith("1454:restart")  # answers 503 on purpose

    def on_console(m):
        if m.type in ("error", "warning") and not (restart and m.text.startswith("Failed to load resource")):
            problems.append(f"{name} console.{m.type}: {m.text}")

    pg.on("console", on_console)
    pg.on("pageerror", lambda e: problems.append(f"{name} pageerror: {e}"))
    pg.route("**/api/**", route_factory(overrides or {}))
    return ctx, pg


def check_layout(pg, name):
    sw = pg.evaluate("document.documentElement.scrollWidth")
    w = pg.evaluate("window.innerWidth")
    if sw > w + 1:
        offenders = pg.evaluate(
            "() => [...document.querySelectorAll('body *')].filter(e => e.getBoundingClientRect().right > window.innerWidth + 1 && !e.closest('.scroll-x,.event-box,.cols,[hidden]')).slice(0,5).map(e => e.tagName + '#' + e.id + '.' + e.className)"
        )
        problems.append(f"{name}: horizontal scroll {sw}px > {w}px; offenders {offenders}")
    bad = pg.evaluate(CONTRAST_JS)
    if bad:
        problems.append(f"{name}: contrast {len(bad)} low: {bad[:6]}")


def click_everything(pg, name):
    """Click every visible button once (re-querying as the DOM re-renders); links/navigations are skipped."""
    seen = set()
    for _ in range(80):
        btns = pg.locator("button:visible:not([role=tab]):not(#actions-btn)")
        n = btns.count()
        target = None
        for i in range(n):
            b = btns.nth(i)
            try:
                key = (
                    (b.get_attribute("id") or "")
                    + "|"
                    + (b.inner_text(timeout=500)[:30])
                    + "|"
                    + (b.get_attribute("data-i") or "")
                    + (b.get_attribute("data-opt") or "")
                    + (b.get_attribute("data-type") or "")
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
            with pg.expect_download(timeout=300):
                target.click(timeout=2000)
        except Exception:
            try:
                target.click(timeout=2000)
            except Exception as e:
                if "Timeout" not in str(e):
                    problems.append(f"{name}: click failed {str(e)[:80]}")
        pg.wait_for_timeout(80)
        if pg.locator("dialog[open]").count():
            pg.keyboard.press("Escape")
        if pg.locator("#actions-menu:visible").count():
            pg.keyboard.press("Escape")
    return len(seen)


def run_matrix(browser):
    for label, vp, scheme in [
        ("desktop-light", {"width": 1280, "height": 1000}, "light"),
        ("desktop-dark", {"width": 1280, "height": 1000}, "dark"),
        ("phone-light", {"width": 390, "height": 900}, "light"),
        ("phone-dark", {"width": 390, "height": 900}, "dark"),
    ]:
        ctx, pg = new_page(browser, vp, scheme, name=label)
        pg.goto(BASE)
        pg.wait_for_timeout(900)
        for tab in ["status", "analysis", "debug", "settings"]:
            pg.goto(f"{BASE}#{tab}")
            pg.reload()
            pg.wait_for_timeout(1500)
            name = f"{label}/{tab}"
            if not pg.locator(f"#panel-{tab}").is_visible():
                problems.append(f"{name}: panel not visible")
            check_layout(pg, name)
            pg.screenshot(path=str(OUT / f"r5-{label}-{tab}.png"), full_page=True)
        if label in ("desktop-light", "phone-dark"):
            for tab in ["status", "analysis", "debug", "settings"]:
                pg.goto(f"{BASE}#{tab}")
                pg.reload()
                pg.wait_for_timeout(1200)
                if tab == "status":
                    # The chart key starts folded at every width: open it (as a person would) so its chips get clicked too.
                    summ = pg.locator("#chart-key > summary")
                    if not summ.is_visible():
                        problems.append(f"{label}/status: the chart key's fold control is not visible")
                    elif pg.evaluate("document.getElementById('chart-key').open") is False:
                        summ.click()
                        pg.wait_for_timeout(150)
                        if summ.get_attribute("aria-expanded") != "true":
                            problems.append(f"{label}/status: the chart key did not open (aria-expanded)")
                n = click_everything(pg, f"{label}/{tab}")
                print(f"{label}/{tab}: clicked {n} controls")
                check_layout(pg, f"{label}/{tab}/after-clicks")
        ctx.close()


def banner_cases(browser):
    base = json.loads(json.dumps(FX["status"]))
    base.update(
        {"manual_override_active": False, "grace_active": False, "promise_summary": {"promises": [], "total_hits": 0}}
    )
    cases = {
        "automation off": ({"automation_enabled": False}, "Automation is off"),
        "vacation": ({"occupancy_mode": "vacation"}, "Vacation mode"),
        "away": ({"occupancy_mode": "away"}, "Away"),
        "weather outage": (
            {
                "entity_health_issues": [
                    {
                        "friendly_name": "Weather source",
                        "entity_id": "weather.home",
                        "status": "unavailable",
                        "detail": "",
                    }
                ]
            },
            "Weather source problem",
        ),
        "sensor outage": (
            {
                "entity_health_issues": [
                    {
                        "friendly_name": "Outdoor temperature sensor",
                        "entity_id": "sensor.out",
                        "status": "unavailable",
                        "detail": "",
                    }
                ]
            },
            "Sensor problem: Outdoor temperature sensor",
        ),
        "unreadable contact": (
            {
                "contact_sensors": [
                    {
                        "entity_id": "b.c",
                        "friendly_name": "Back door",
                        "unreadable": True,
                        "last_reading": "never",
                        "treated_as": "closed",
                    }
                ]
            },
            "Sensor problem: Back door",
        ),
        "paused by door": ({"paused_by_door": True}, "Paused: a door or window is open"),
        "override": ({"manual_override_active": True, "grace_active": True}, "You changed the thermostat"),
        "grace only": ({"grace_active": True}, "A grace period is active"),
        "fan override": ({"fan_override_active": True}, "Fan override is active"),
        "fan off hold": ({"fan_off_hold_active": True}, "Fan-off pause is active"),
        "unhealthy": ({"coordinator_healthy": False, "last_error": "boom"}, "Climate Advisor is unavailable"),
        "problem": (
            {"hvac_control_issues": [{"issue": "x", "detail": "Thermostat is not following"}]},
            "Thermostat is not following",
        ),
        "promises": (
            {
                "promise_summary": {
                    "promises": [{"promise": "a", "label": "7: comfort kept", "hits": 2}],
                    "total_hits": 2,
                }
            },
            "household promises missed",
        ),
    }
    for label, (patch, expect) in cases.items():
        st = dict(base)
        st.update(patch)
        ctx, pg = new_page(browser, {"width": 1280, "height": 900}, "light", {"status": st}, name=f"banner:{label}")
        pg.goto(BASE)
        pg.wait_for_timeout(1000)
        txt = pg.locator("#banners").inner_text()
        if expect not in txt:
            problems.append(f"banner '{label}': expected '{expect}' in {txt!r}")
        if label == "promises":  # promise banner is Status-only
            pg.goto(f"{BASE}#analysis")
            pg.reload()
            pg.wait_for_timeout(600)
            if "promises missed" in pg.locator("#banners").inner_text():
                problems.append("promise banner should be Status-only")
        if label == "automation off":
            pg.screenshot(path=str(OUT / "r5-banners.png"), full_page=False)
            before = len(requests)
            pg.get_by_role("button", name="Turn automation on").click()
            pg.wait_for_timeout(300)
            if not any(m == "POST" and e == "toggle_automation" for m, e, q, b in requests[before:]):
                problems.append("banner action did not POST toggle_automation")
        ctx.close()
    # reload needed comes from settings_extra
    ex = dict(FX["settings_extra"], reload_needed=True)
    ctx, pg = new_page(browser, {"width": 1280, "height": 900}, "light", {"settings_extra": ex}, name="banner:reload")
    pg.goto(BASE)
    pg.wait_for_timeout(1000)
    if "waiting for a reload" not in pg.locator("#banners").inner_text():
        problems.append("reload-needed banner missing")
    ctx.close()


def behaviour(browser):
    learning = json.loads(json.dumps(FX["learning"]))
    learning["suggestions"] = [{"key": "k1", "text": "Raise the sleep setpoint"}]
    ctx, pg = new_page(browser, {"width": 1280, "height": 900}, "light", {"learning": learning}, name="behaviour")
    pg.goto(BASE)
    pg.wait_for_timeout(800)
    # keyboard: tablist arrows
    pg.locator("#tab-btn-status").focus()
    pg.keyboard.press("ArrowRight")
    pg.wait_for_timeout(300)
    if pg.locator("#tab-btn-analysis").get_attribute("aria-selected") != "true" or "#analysis" not in pg.url:
        problems.append("ArrowRight did not select Analysis / set the hash")
    pg.keyboard.press("End")
    pg.wait_for_timeout(300)
    if pg.locator("#tab-btn-settings").get_attribute("aria-selected") != "true":
        problems.append("End did not select Settings")
    # actions menu keyboard
    pg.locator("#actions-btn").focus()
    pg.keyboard.press("Enter")
    if pg.locator("#actions-btn").get_attribute("aria-expanded") != "true":
        problems.append("Actions menu did not open on Enter")
    pg.keyboard.press("ArrowDown")
    pg.keyboard.press("Escape")
    if pg.locator("#actions-menu").is_visible():
        problems.append("Escape did not close the Actions menu")
    pg.screenshot(path=str(OUT / "r5-actions-closed.png"))
    # actions post the classic endpoints
    for label, ep in [
        ("Re-plan (reclassify now)", "force_reclassify"),
        ("Send briefing", "send_briefing"),
        ("Pause all automation", "toggle_automation"),
    ]:
        before = len(requests)
        pg.locator("#actions-btn").click()
        pg.get_by_role("menuitem", name=label).click()
        pg.wait_for_timeout(250)
        if not any(m == "POST" and e == ep for m, e, q, b in requests[before:]):
            problems.append(f"Actions > {label} did not POST {ep}")
    pg.locator("#actions-btn").click()
    pg.screenshot(path=str(OUT / "r5-actions-menu.png"))
    pg.keyboard.press("Escape")
    # deep links
    pg.goto(f"{BASE}#debug")
    pg.reload()
    pg.wait_for_timeout(900)
    if not pg.locator("#panel-debug").is_visible():
        problems.append("#debug deep link failed")
    pg.goto(f"{BASE}#settings/comfort")
    pg.reload()
    pg.wait_for_timeout(1500)
    if not pg.locator("#settings-comfort").is_visible():
        problems.append("#settings/comfort deep link: group not shown")
    # settings: search, changed markers, descriptions, read-only
    before = len(requests)
    pg.fill("#set-search", "grace")
    pg.wait_for_timeout(200)
    shown = pg.locator("#set-groups section").count()
    pg.fill("#set-search", "")
    pg.check("#set-changed")
    pg.wait_for_timeout(200)
    changed_rows = pg.locator("#set-groups .badge.changed").count()
    if changed_rows < 1:
        problems.append("changed-from-default markers missing")
    pg.uncheck("#set-changed")
    pg.check("#set-desc")
    pg.screenshot(path=str(OUT / "r5-settings-desc.png"), full_page=True)
    writes = [(m, e) for m, e, q, b in requests[before:] if m != "GET"]
    if writes or shown < 1:
        problems.append(f"settings interactions wrote {writes} / search shown {shown}")
    # analysis: suggestion accept uses classic body; report flow
    pg.goto(f"{BASE}#analysis")
    pg.reload()
    pg.wait_for_timeout(1500)
    before = len(requests)
    pg.get_by_role("button", name="Accept").click()
    pg.wait_for_timeout(300)
    posts = [
        (e, json.loads(b)) for m, e, q, b in requests[before:] if m == "POST" and e.startswith("respond_suggestion")
    ]
    if posts != [("respond_suggestion" if not posts else posts[0][0], {"action": "accept", "suggestion_key": "k1"})]:
        problems.append(f"accept suggestion body differs from classic: {posts}")
    pg.click("#run-report-btn")
    pg.wait_for_timeout(500)
    if "override" not in pg.locator("#report-content").inner_text():
        problems.append("activity report did not render")
    if not pg.locator("#report-actions").is_visible():
        problems.append("report actions hidden after generate")
    pg.click("#rep-issue")
    pg.wait_for_timeout(200)
    if not pg.locator("dialog[open]").count():
        problems.append("GitHub modal did not open")
    pg.screenshot(path=str(OUT / "r5-github-modal.png"))
    before = len(requests)
    pg.click("#github-submit-btn")
    pg.wait_for_timeout(500)
    if not any(e.startswith("submit_github_issue") for m, e, q, b in requests[before:]):
        problems.append("GitHub submit did not POST submit_github_issue")
    pg.keyboard.press("Escape")
    # AI investigation switch shows focus controls
    pg.select_option("#report-type", "investigation")
    if not pg.locator("#investigation-focus-controls").is_visible():
        problems.append("investigation controls hidden")
    # prediction accuracy iframe loads and its header is hidden
    pg.wait_for_timeout(800)
    fr = pg.frame_locator("#pa-frame")
    if fr.locator("#pa-chart").count() != 1:
        problems.append("embedded prediction accuracy chart missing")
    # debug: search + event filter
    pg.goto(f"{BASE}#debug")
    pg.reload()
    pg.wait_for_timeout(1500)
    groups = pg.locator("#dbg-groups section").count()
    pg.fill("#dbg-search", "fan_")
    pg.wait_for_timeout(200)
    narrowed = pg.locator("#dbg-groups section").count()
    if not (0 < narrowed < groups):
        problems.append(f"debug search did not narrow groups ({groups} -> {narrowed})")
    pg.fill("#dbg-search", "")
    n_all = pg.locator("#dbg-ev-rows tr").count()
    pg.locator("#dbg-ev-types button").first.click()
    n_one = pg.locator("#dbg-ev-rows tr").count()
    if not n_one < n_all:
        problems.append("event type filter did not hide rows")
    # zone switch carries entry_id
    before = len(requests)
    pg.select_option("#zone-select", "zone-2")
    pg.wait_for_timeout(800)
    if not any("entry_id=zone-2" in q for m, e, q, b in requests[before:]):
        problems.append("zone switch did not send entry_id=zone-2")
    # back to classic remembers the choice
    pg.evaluate("document.getElementById('back-classic').addEventListener('click', e => e.preventDefault())")
    pg.click("#back-classic")
    if pg.evaluate("localStorage.getItem('climate_advisor_dashboard')") != "classic":
        problems.append("Back to classic did not remember the choice")
    ctx.close()
    # night card on the live plan: no sample badge, options toggle, steps listed; then the plan without figures; then empty
    ctx, pg = new_page(browser, {"width": 1280, "height": 900}, "dark", name="night")
    pg.goto(BASE)
    pg.wait_for_timeout(1200)
    if pg.locator("#night-card svg[role=img]").count() != 1:
        problems.append("night card chart missing")
    if pg.locator("#night-sample-badge").is_visible():
        problems.append("the sample badge is still shown on the live night plan")
    pg.locator("#night-card [data-opt]").first.click()
    pg.wait_for_timeout(200)
    pg.screenshot(path=str(OUT / "r5-night-card-dark.png"), full_page=True)
    ctx.close()
    ctx, pg = new_page(
        browser, {"width": 1280, "height": 900}, "light", {"night_strategy": live_night(False)}, name="night-nonumbers"
    )
    pg.goto(BASE)
    pg.wait_for_timeout(1200)
    # The headline may name the CONFIGURED sleep-band setpoint CA holds ("keeps the heat for the bedroom at 66°F", built
    # server-side by night_strategy.headline from the band, not from v4); C11 hides PREDICTED figures, so only the rest
    # of the card must be free of temperatures.
    card_text = pg.locator("#night-card").inner_text()
    if pg.locator("#night-headline").count():
        card_text = card_text.replace(pg.locator("#night-headline").inner_text(), "")
    if pg.locator("#night-card svg[role=img]").count() or "°" in card_text:
        problems.append("night card shows figures although show_numbers is false")
    if not pg.locator("#night-nonumbers").count():
        problems.append("night card without figures lacks its explanation")
    ctx.close()


def accuracy_without_v4(browser):
    """Issue #1400: with an empty v4 archive the accuracy page still scores v3 and "no change" and lists "_ca" once.
    Screenshots: pa-no-v4-<scheme>-<width>.png."""
    d = json.loads(PA)
    d["points"] = [{k: v for k, v in p.items() if k != "v4"} for p in d["points"]]
    d["stats"] = {**d["stats"], "n": 0, "v4_mae": None, "v4_bias": None}
    d["sensors"] = [d["sensors"][0], {"id": "_ca", "name": "_ca", "role": "room sensor"}, *d["sensors"][1:]]
    for scheme, vp in (("light", {"width": 1280, "height": 1000}), ("dark", {"width": 390, "height": 900})):
        name = f"pa-no-v4:{scheme}:{vp['width']}"
        ctx, pg = new_page(browser, vp, scheme, {"prediction_accuracy": d}, name=name)
        pg.goto(f"http://127.0.0.1:{PORT}/prediction_accuracy.html")
        pg.wait_for_timeout(1200)
        tiles = pg.locator("#tiles").inner_text()
        for want in ("v3 average miss", f"{d['stats']['v3_mae']:.2f}", "No change", f"{d['stats']['flat_mae']:.2f}"):
            if want not in tiles:
                problems.append(f"{name}: tiles lack {want!r}: {tiles!r}")
        if "no v4 predictions scored here yet" not in tiles:
            problems.append(f"{name}: v4 tile does not say it has nothing yet")
        names = pg.locator("#grp-sensor button").all_inner_texts()
        if names.count("Climate Advisor reading") != 1 or "_ca" in names:
            problems.append(f"{name}: sensor list {names}")
        if pg.evaluate("document.documentElement.scrollWidth") > vp["width"]:
            problems.append(f"{name}: horizontal scroll")
        pg.screenshot(path=str(OUT / f"pa-no-v4-{scheme}-{vp['width']}.png"), full_page=True)
        ctx.close()


SEGMENTS_JS = """
() => window.__paLines().map(l => {
  const seg = [];
  for (let i = 1; i < l.data.length; i++) if (l.data[i - 1] != null && l.data[i] != null) seg.push([l.data[i - 1], l.data[i]]);
  return { label: l.label, n: l.data.filter(x => x != null).length, breaks: l.data.filter(x => x == null).length, seg };
})
"""


def accuracy_1431(browser):
    """Issue #1431: engine roles in the labels, an explanation line, lines broken at the sensor swaps, joined hourly
    points, a labelled no-v4 sensor and the dashboards' subtitle. Screenshots: pa-1431-<case>-<scheme>-<width>.png."""
    d = json.loads(PA)
    swaps = [dt.datetime.fromisoformat(t).timestamp() * 1000 for t in d["ca_swaps"]]
    for scheme, vp in (("light", {"width": 1280, "height": 1000}), ("dark", {"width": 390, "height": 900})):
        name = f"pa-1431:{scheme}:{vp['width']}"
        ctx, pg = new_page(browser, vp, scheme, name=name)
        pg.goto(f"http://127.0.0.1:{PORT}/prediction_accuracy.html")
        pg.wait_for_timeout(1200)
        want_v4 = "v4 (main engine)" if d["primary_engine"] == "v4" else "v4 (comparison)"
        if pg.locator("#legend-v4-name").inner_text() != want_v4:
            problems.append(f"{name}: v4 legend {pg.locator('#legend-v4-name').inner_text()!r}, want {want_v4!r}")
        body = pg.locator("body").inner_text()
        for stale in ("today\u2019s line", "(new)", "Preview", "preview", "runs in shadow"):
            if stale in body:
                problems.append(f"{name}: stale wording {stale!r} on the page")
        if "Each point is a past prediction" not in pg.locator("#pa-explain").inner_text():
            problems.append(f"{name}: no explanation line under the chart")
        lines = pg.evaluate(SEGMENTS_JS)
        for ln in lines:
            for a, b in ln["seg"]:
                if any(a < s <= b for s in swaps):
                    problems.append(f"{name}: {ln['label']} crosses a sensor swap")
                    break
            if ln["n"] > 1 and not ln["seg"]:
                problems.append(f"{name}: {ln['label']} is drawn as dots only ({ln['n']} hourly points)")
        if not pg.locator("#legend-swap").is_visible():
            problems.append(f"{name}: swap marks not in the key")
        bed = pg.locator('#grp-sensor button[data-value="sensor.bedroom_temp"]')
        if "no v4 data yet" not in bed.inner_text() or "#1422" not in (bed.get_attribute("title") or ""):
            problems.append(f"{name}: bedroom button lacks 'no v4 data yet' / its reason")
        if pg.evaluate("document.documentElement.scrollWidth") > vp["width"]:
            problems.append(f"{name}: horizontal scroll")
        pg.screenshot(path=str(OUT / f"pa-1431-swaps-{scheme}-{vp['width']}.png"), full_page=True)
        ctx.close()
        # the bedroom (no v4): words instead of a missing line
        nv = json.loads(PA)
        nv["points"] = [{k: v for k, v in q.items() if k not in ("v4", "v3")} for q in nv["points"]]
        nv["sensor"] = "sensor.bedroom_temp"
        ctx, pg = new_page(browser, vp, scheme, {"prediction_accuracy": nv}, name=name + ":bedroom")
        pg.goto(f"http://127.0.0.1:{PORT}/prediction_accuracy.html")
        pg.wait_for_timeout(1000)
        pg.locator('#grp-sensor button[data-value="sensor.bedroom_temp"]').click()
        pg.wait_for_timeout(600)
        msg = pg.locator("#pa-nov4")
        if not msg.is_visible() or "v4 hasn\u2019t learned this sensor yet (see #1422)" not in msg.inner_text():
            problems.append(f"{name}: no-v4 sensor does not say so in words")
        pg.screenshot(path=str(OUT / f"pa-1431-no-v4-sensor-{scheme}-{vp['width']}.png"), full_page=True)
        ctx.close()
    # the dashboards: no "Preview: v4 runs in shadow"
    ctx, pg = new_page(browser, {"width": 1280, "height": 900}, "light", name="pa-1431:analysis")
    pg.goto(f"{BASE}#analysis")
    pg.wait_for_timeout(1500)
    sub = pg.locator('section[aria-labelledby="an-pa-h"] .sub').inner_text()
    if "Preview" in sub or "shadow" in sub:
        problems.append(f"analysis accuracy subtitle still pre-O3: {sub!r}")
    pg.locator("#pa-frame").scroll_into_view_if_needed()
    pg.wait_for_timeout(800)
    pg.screenshot(path=str(OUT / "pa-1431-analysis-embedded.png"), full_page=True)
    ctx.close()


def restart_1454(browser):
    """Issue #1454: a restart (status 503 twice, then answers) and a first visit (nothing stored): the page ends up
    on the zone's data and sends no Climate Advisor request without a zone (only GET zones names none)."""
    left = {"n": 2}

    def status():
        if left["n"] > 0:
            left["n"] -= 1
            return 503, {"error": "Climate Advisor not loaded"}
        return 200, FX["status"]

    for case, overrides in (("restart", {"status": status}), ("first-visit", {})):
        before = len(requests)
        ctx, pg = new_page(browser, {"width": 1280, "height": 900}, "light", overrides, name=f"1454:{case}")
        if case == "restart":
            pg.add_init_script("localStorage.setItem('climate_advisor_selected_zone', 'zone-2')")
        pg.goto(BASE)
        try:
            pg.wait_for_function(
                "document.getElementById('health-text').textContent.indexOf('Healthy') === 0", timeout=20000
            )
        except Exception:  # noqa: BLE001
            problems.append(f"1454 {case}: never showed the zone's status: {pg.locator('#health-text').inner_text()!r}")
        for tab in ("analysis", "debug", "settings", "status"):
            pg.locator(f"#tab-btn-{tab}").click()
            pg.wait_for_timeout(500)
        unscoped = [
            f"{m} {e}?{q}"
            for m, e, q, _b in requests[before:]
            if e != "zones" and "entry_id=" not in q and "zone=" not in q
        ]
        if unscoped:
            problems.append(f"1454 {case}: unscoped requests: {unscoped}")
        want = "zone-2" if case == "restart" else "zone-1"
        if pg.evaluate("localStorage.getItem('climate_advisor_selected_zone')") != want:
            problems.append(f"1454 {case}: stored zone is not {want}")
        pg.screenshot(path=str(OUT / f"1454-{case}.png"), full_page=False)
        ctx.close()


with sync_playwright() as p:
    b = p.chromium.launch()
    restart_1454(b)
    accuracy_without_v4(b)
    accuracy_1431(b)
    run_matrix(b)
    banner_cases(b)
    behaviour(b)
    b.close()

_unscoped = sorted(
    {f"{m} {e}" for m, e, q, _b in requests if e != "zones" and "entry_id=" not in q and "zone=" not in q}
)
if _unscoped:  # Issue #1454: every dashboard request names a zone
    problems.append(f"unscoped Climate Advisor requests: {_unscoped}")
print("\n".join(problems) or "no problems")
print("distinct endpoints:", sorted({e.split("?")[0] for m, e, q, bd in requests})[:30])
sys.exit(1 if problems else 0)
