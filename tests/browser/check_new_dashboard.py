# ruff: noqa: E501
"""Real-browser check of the new dashboard (Issue #1230 R5). Not collected by pytest (no JS runtime in CI).

    pwvenv/bin/python tests/browser/check_new_dashboard.py <repo-root> <screenshot-dir>

Serves the frontend folder, answers /api/** from tests/fixtures/new_dashboard_api.json (shapes captured from the
real endpoints), sets hassTokens, and for 1280/390 px x light/dark: opens every tab, clicks every visible button,
checks for console errors, horizontal scroll and text contrast (>= 4.5:1, 3:1 for large text). Also drives the
banners, deep links, keyboard model, zone switching and the "no new write path" rule.
"""

from __future__ import annotations

import functools
import http.server
import json
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


def route_factory(overrides: dict):
    def route(r):
        u = urllib.parse.urlparse(r.request.url)
        ep = u.path.split("/api/climate_advisor/")[-1]
        body = r.request.post_data or ""
        requests.append((r.request.method, ep, u.query, body))
        if ep in overrides:
            payload = overrides[ep]
        elif ep == "prediction_accuracy":
            r.fulfill(status=200, content_type="application/json", body=PA)
            return
        elif r.request.method == "POST":
            payload = {"message": "ok", "success": True, "automation_enabled": False}
        elif ep == "chart_data":
            payload = FX["chart_data"]
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
    pg.on(
        "console",
        lambda m: problems.append(f"{name} console.{m.type}: {m.text}") if m.type in ("error", "warning") else None,
    )
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
    # night card (sample data)
    ctx, pg = new_page(browser, {"width": 1280, "height": 900}, "dark", name="night")
    pg.goto(BASE)
    pg.wait_for_timeout(1200)
    if pg.locator("#night-card svg[role=img]").count() != 1:
        problems.append("night card chart missing")
    pg.locator("#night-card [data-opt]").first.click()
    pg.get_by_role("button", name="Warm night example: Aug 25 (actual outdoor)").click()
    pg.wait_for_timeout(200)
    pg.screenshot(path=str(OUT / "r5-night-card-dark.png"), full_page=True)
    ctx.close()


with sync_playwright() as p:
    b = p.chromium.launch()
    run_matrix(b)
    banner_cases(b)
    behaviour(b)
    b.close()

print("\n".join(problems) or "no problems")
print("distinct endpoints:", sorted({e.split("?")[0] for m, e, q, bd in requests})[:30])
sys.exit(1 if problems else 0)
