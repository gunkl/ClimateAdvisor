# ruff: noqa: E501, F821
"""Real-browser check of the right-to-left layout and the viewer-language dashboard (Issue #1341, PR C). Not collected by pytest. Also Traditional Chinese (Issue #1656).

    pwvenv/bin/python tests/browser/check_i18n_rtl.py <repo-root> <screenshot-dir>

Reuses check_new_dashboard.py's server, API stubs and page factory (everything above its run_matrix), then opens Status
(chart and night card), Settings, Analysis (after generating the Activity record) and the Prediction accuracy page in Arabic
(right-to-left) and English at 1280 / 390 px, light and dark. Per view: no console error, no horizontal scroll, no
element whose box overflows, and every piece of text inside its card / tile / button. Screenshots land in the directory.
"""

from __future__ import annotations

import pathlib
import sys

from playwright.sync_api import sync_playwright

HERE = pathlib.Path(__file__).resolve().parent
_src = (HERE / "check_new_dashboard.py").read_text(encoding="utf-8")
exec(compile(_src.split("def run_matrix(browser):")[0], "check_new_dashboard_head", "exec"))  # noqa: S102

OVERFLOW_JS = """
() => {
  const out = [];
  const de = document.documentElement;
  if (de.scrollWidth > innerWidth + 1) out.push('page scrolls sideways: ' + de.scrollWidth + ' > ' + innerWidth);
  const inScroller = (e) => { for (let a = e; a && a !== document.body; a = a.parentElement) { const o = getComputedStyle(a).overflowX; if ((o === 'auto' || o === 'scroll') && a.scrollWidth > a.clientWidth) return true; if (a.matches('.scroll-x,.event-box,.chart-table-scroll')) return true; } return false; };
  const visible = (e) => { const r = e.getBoundingClientRect(); const s = getComputedStyle(e); return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && !e.closest('[hidden]'); };
  // 1. a box that spills out of its page
  for (const e of document.body.querySelectorAll('*')) {
    if (!visible(e) || inScroller(e) || e.closest('svg,canvas,script,style,option,select')) continue;
    const r = e.getBoundingClientRect();
    if (r.right > innerWidth + 1 || r.left < -1) out.push('off-screen: ' + e.tagName.toLowerCase() + (e.id ? '#' + e.id : '') + '.' + String(e.className).slice(0, 30));
  }
  // 2. text that is wider than the box clipping it, or outside the card / tile / button holding it
  const w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  for (let n = w.nextNode(); n; n = w.nextNode()) {
    if (!n.nodeValue.trim()) continue;
    const el = n.parentElement;
    if (!el || !visible(el) || el.closest('script,style,option,select,canvas,svg,.sr-only') || el.clientWidth <= 2 || inScroller(el)) continue; // a screen-reader-only label is meant to be 1 px
    const rg = document.createRange(); rg.selectNodeContents(n);
    const tr = rg.getBoundingClientRect(); if (!tr.width) continue;
    const box = el.closest('.card,.tile,.chip,button,.badge,td,th,li,.banner,.note-box,.rec,.row');
    if (box && visible(box)) {
      const b = box.getBoundingClientRect();
      if (tr.left < b.left - 1.5 || tr.right > b.right + 1.5) out.push('text outside ' + box.tagName.toLowerCase() + (box.id ? '#' + box.id : '') + ': "' + n.nodeValue.trim().slice(0, 40) + '"');
    }
    const cs = getComputedStyle(el);
    if ((cs.overflowX === 'hidden' || cs.overflowX === 'clip') && cs.textOverflow !== 'ellipsis' && el.scrollWidth > el.clientWidth + 1) out.push('clipped text: "' + n.nodeValue.trim().slice(0, 40) + '"');
  }
  return [...new Set(out)].slice(0, 12);
}
"""

SHOTS = pathlib.Path(sys.argv[2])
failures: list[str] = []
count = 0


def check(label: str, pg) -> None:
    global count
    count += 1
    for m in pg.evaluate(OVERFLOW_JS):
        failures.append(f"{label}: {m}")


def shot(pg, label: str, selector: str | None = None) -> None:
    path = SHOTS / f"{label}.png"
    if selector and pg.locator(selector).count():
        pg.locator(selector).first.screenshot(path=str(path))
    else:
        pg.screenshot(path=str(path), full_page=True)


def run(browser) -> None:
    for lang in ("ar", "en", "zh-Hant"):  # zh-Hant (#1656): Chinese line breaking and width, left to right
        for vlabel, vp in (("1280", {"width": 1280, "height": 1000}), ("390", {"width": 390, "height": 900})):
            for scheme in ("light", "dark"):
                tag = f"{lang}-{vlabel}-{scheme}"
                ctx, pg = new_page(browser, vp, scheme, name=tag)
                url = f"{BASE}?lang={lang}"
                pg.goto(url + "#status")
                pg.wait_for_timeout(1800)
                exp_dir = "rtl" if lang == "ar" else "ltr"
                got = pg.evaluate("document.documentElement.dir")
                if got != exp_dir:
                    failures.append(f"{tag}: <html dir> is {got}, expected {exp_dir}")
                # the page really is mirrored: the brand sits at the leading (right) edge, the first tile of a row is right of the second
                geo = pg.evaluate(
                    "() => { const b = document.querySelector('.brand').getBoundingClientRect(); const t = [...document.querySelectorAll('#status-grid .tile')].map(e => e.getBoundingClientRect()); const row = t.filter(r => Math.abs(r.top - t[0].top) < 2);"
                    " return { brandMid: (b.left + b.right) / 2 / innerWidth, firstRight: row.length > 1 ? row[0].left > row[1].left : null, chartLtr: getComputedStyle(document.querySelector('.chart-stack')).direction }; }"
                )
                if lang == "ar":
                    if geo["brandMid"] < 0.5:
                        failures.append(f"{tag}: the brand is not at the right edge ({geo['brandMid']:.2f})")
                    if geo["firstRight"] is False:
                        failures.append(f"{tag}: the first Status tile is not at the right of its row")
                if geo["chartLtr"] != "ltr":
                    failures.append(f"{tag}: the chart stack is {geo['chartLtr']} (its time axis runs left to right)")
                check(f"{tag}/status", pg)
                shot(pg, f"{tag}-status")
                shot(pg, f"{tag}-chart", ".card:has(.chart-stack)")
                shot(pg, f"{tag}-night", "#night-card")
                for tab in ("settings", "analysis"):
                    pg.goto(f"{url}#{tab}")
                    pg.reload()
                    pg.wait_for_timeout(1500)
                    if tab == "analysis":
                        pg.select_option("#report-type", "activity_record")
                        pg.click("#run-report-btn")
                        pg.wait_for_timeout(800)
                    check(f"{tag}/{tab}", pg)
                    shot(pg, f"{tag}-{tab}")
                pg.goto(f"http://127.0.0.1:{PORT}/prediction_accuracy.html?lang={lang}")
                pg.wait_for_timeout(1800)
                got = pg.evaluate("document.documentElement.dir")
                if got != exp_dir:
                    failures.append(f"{tag}/pa: <html dir> is {got}, expected {exp_dir}")
                check(f"{tag}/pa", pg)
                shot(pg, f"{tag}-pa")
                ctx.close()


with sync_playwright() as p:
    b = p.chromium.launch()
    run(b)
    b.close()

console = [x for x in problems if "contrast" not in x and "horizontal scroll" not in x]
print(f"{count} views checked; {len(failures)} layout problems; {len(console)} console problems")
for x in failures + console:
    print("  " + x)
sys.exit(1 if failures or console else 0)
