"""
take_screenshots.py — Capture CA dashboard screenshots via Playwright.

Injects HA auth, locates the CA iframe, and saves screenshots of key views
with the HA sidebar cropped out.

Usage:
  python tools/take_screenshots.py
  python tools/take_screenshots.py --tabs forecast_3d forecast_24h status ai
  python tools/take_screenshots.py --out docs/screenshots --visible

Requires: playwright  (pip install playwright && playwright install chromium)
Config:   .deploy.env with HA_HOST and HA_API_TOKEN
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

HA_HOST = "homeassistant.local"
HA_PORT = 8123
HA_TOKEN = ""

WINDOW_W = 1440
WINDOW_H = 940


def load_env() -> None:
    for f in [Path(".deploy.env"), Path(".env")]:
        if f.exists():
            for line in f.read_text().splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    os.environ.setdefault(k.strip(), v.strip())
            break


async def capture(output_dir: Path, tab_names: list[str], visible: bool) -> list[Path]:
    from playwright.async_api import async_playwright

    output_dir.mkdir(parents=True, exist_ok=True)
    saved: list[Path] = []
    panel_url = f"http://{HA_HOST}:{HA_PORT}/climate-advisor"
    print(f"Target: {panel_url}")

    # Auth token payload — same format as HA frontend expects
    tokens = {
        "access_token": HA_TOKEN,
        "token_type": "Bearer",
        "expires_in": 1800,
        "hassUrl": f"http://{HA_HOST}:{HA_PORT}",
        "expires": 9_999_999_999_999,
    }

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=not visible)
        ctx = await browser.new_context(
            viewport={"width": WINDOW_W, "height": WINDOW_H},
            ignore_https_errors=True,
        )
        # Inject auth on every page load before scripts run
        await ctx.add_init_script(f"localStorage.setItem('hassTokens', JSON.stringify({json.dumps(tokens)}))")

        page = await ctx.new_page()

        # ── Navigate to CA panel ────────────────────────────────────────
        print("Loading CA panel...")
        await page.goto(panel_url, wait_until="networkidle", timeout=30000)
        await page.wait_for_timeout(3000)
        print(f"  URL: {page.url}")

        # ── Find the CA iframe (index.html) ─────────────────────────────
        ca_frame = next(
            (f for f in page.frames if "climate_advisor/frontend/index.html" in f.url),
            None,
        )
        if ca_frame is None:
            print("ERROR: CA iframe not found. Frames:")
            for f in page.frames:
                print(f"  {f.url}")
            await browser.close()
            return []
        print(f"  CA frame: {ca_frame.url[:70]}")

        # ── Wait for CA content ─────────────────────────────────────────
        try:
            await ca_frame.wait_for_selector(".tab-btn[data-tab='status']", timeout=12000)
            print("  CA dashboard ready")
        except Exception:
            print("  WARNING: CA tabs slow to appear — continuing")
        await page.wait_for_timeout(1000)

        # ── Determine HA sidebar width (for full-page clip) ─────────────
        sidebar_right = await page.evaluate("""() => {
            const s = document.querySelector('ha-sidebar, .sidebar, aside');
            return s ? Math.round(s.getBoundingClientRect().right) : 245;
        }""")
        sx = int(sidebar_right)
        cw = WINDOW_W - sx
        print(f"  Sidebar ends at x={sx}, content width={cw}px")

        # ── Activate Status tab + wait for chart to load ─────────────────
        await ca_frame.click(".tab-btn[data-tab='status']")
        await page.wait_for_timeout(2500)
        canvas_h = await ca_frame.evaluate(
            "() => { const c = document.querySelector('canvas'); return c ? c.offsetHeight : 0; }"
        )
        print(f"  Chart canvas: {canvas_h}px tall")

        # ── Capture each view ────────────────────────────────────────────
        for tab in tab_names:
            print(f"\n[{tab}]")
            out = output_dir / f"{tab}.png"

            if tab in ("forecast_3d", "forecast_24h"):
                rng = "3d" if tab == "forecast_3d" else "24h"
                await _forecast(page, ca_frame, rng, out, sx, cw)

            elif tab == "status":
                # Top of Status tab — shows current status grid + strategy
                await ca_frame.click(".tab-btn[data-tab='status']")
                await page.wait_for_timeout(1200)
                await page.screenshot(
                    path=str(out),
                    clip={"x": sx, "y": 0, "width": cw, "height": WINDOW_H},
                )

            elif tab == "ai":
                await ca_frame.click(".tab-btn[data-tab='ai']")
                await page.wait_for_timeout(2000)
                await page.screenshot(
                    path=str(out),
                    clip={"x": sx, "y": 0, "width": cw, "height": WINDOW_H},
                )

            if out.exists() and out.stat().st_size > 15_000:
                print(f"  Saved {out.name} ({out.stat().st_size // 1024}KB)")
                saved.append(out)
            else:
                sz = out.stat().st_size if out.exists() else 0
                print(f"  FAILED {out.name} ({sz} bytes)")

        await browser.close()

    return saved


async def _forecast(page, ca_frame, range_str: str, out: Path, sx: int, cw: int) -> None:
    """Click a chart range, wait for data, screenshot the chart section."""
    # Make sure Status tab is active
    await ca_frame.click(".tab-btn[data-tab='status']")
    await page.wait_for_timeout(400)

    # Click the range button
    try:
        await ca_frame.click(f".range-btn[data-range='{range_str}']", timeout=6000)
        await page.wait_for_timeout(4000)  # wait for API + chart render
    except Exception as e:
        print(f"  WARNING: range click failed: {e}")

    # Scroll the chart range bar into view and get its position in the iframe
    chart_y_in_frame = await ca_frame.evaluate("""() => {
        const el = document.querySelector('.chart-range-bar');
        if (el) { el.scrollIntoView({behavior:'instant', block:'start'}); }
        return el ? el.getBoundingClientRect().top : 50;
    }""")
    await page.wait_for_timeout(300)

    # Get iframe position within the full HA page
    iframe_rect = await page.evaluate("""() => {
        const f = document.querySelector('iframe[src*="climate_advisor"]');
        return f ? {top: f.getBoundingClientRect().top, left: f.getBoundingClientRect().left} : {top: 0, left: 0};
    }""")

    page_chart_y = max(0, int(iframe_rect["top"]) + int(chart_y_in_frame) - 12)
    height = WINDOW_H - page_chart_y

    await page.screenshot(
        path=str(out),
        clip={"x": sx, "y": page_chart_y, "width": cw, "height": height},
    )


def main() -> int:
    load_env()
    global HA_HOST, HA_PORT, HA_TOKEN
    HA_HOST = os.environ.get("HA_HOST", "homeassistant.local")
    HA_PORT = int(os.environ.get("HA_PORT", "8123"))
    HA_TOKEN = os.environ.get("HA_API_TOKEN", "")

    if not HA_TOKEN:
        print("ERROR: HA_API_TOKEN not set. Check .deploy.env")
        return 1

    all_tabs = ["forecast_3d", "forecast_24h", "status", "ai"]
    parser = argparse.ArgumentParser()
    parser.add_argument("--tabs", nargs="+", default=all_tabs, choices=all_tabs)
    parser.add_argument("--out", default="docs/screenshots")
    parser.add_argument("--visible", action="store_true")
    args = parser.parse_args()

    saved = asyncio.run(capture(Path(args.out), args.tabs, args.visible))
    print(f"\nResult: {len(saved)}/{len(args.tabs)} screenshots saved.")
    for p in saved:
        print(f"  {p}")
    return 0 if len(saved) == len(args.tabs) else 1


if __name__ == "__main__":
    sys.exit(main())
