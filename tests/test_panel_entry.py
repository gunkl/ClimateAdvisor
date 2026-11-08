"""Issue #1230 R6a: the panel opens the new dashboard by default (frontend/entry.html chooser).

Occupant: opening Climate Advisor shows the new dashboard; "Back to classic" is one click and remembered in this
browser; classic stays fully working; deep links and classic links keep working. The chooser decision table runs
for real in a browser (tests/ui/entry.spec.js); here the exact rule and the storage guard are pinned.
"""

from __future__ import annotations

import re
from pathlib import Path

from custom_components.climate_advisor.const import PANEL_FRONTEND_PATH, PANEL_URL
from tools.sim_harness.build_coordinator import build_headless_multi_zone

ROOT = Path(__file__).resolve().parent.parent
FE = ROOT / "custom_components" / "climate_advisor" / "frontend"
ENTRY = (FE / "entry.html").read_text(encoding="utf-8")
CORE = (FE / "nd_core.js").read_text(encoding="utf-8")
NEW = (FE / "new_dashboard.html").read_text(encoding="utf-8")
CLASSIC = (FE / "index.html").read_text(encoding="utf-8")


CHOOSER = (
    "function chooseDashboard(pref, search) {\n"
    "    if (/(?:^|[?&])classic=1(?:&|$)/.test(search || '')) return 'index.html';\n"
    "    return pref === 'classic' ? 'index.html' : 'new_dashboard.html';\n"
    "  }\n"
)


def test_chooser_structure_storage_guard_and_deep_link_preserved():
    # the decision function (its cases run in a real browser by tests/ui/entry.spec.js): only the exact rule below
    assert CHOOSER in ENTRY
    assert "localStorage.getItem('climate_advisor_dashboard')" in ENTRY
    # storage read in try/catch, falls back to no preference (-> new)
    assert re.search(r"try \{ pref = localStorage\.getItem\([^)]*\); \} catch \(e\) \{ pref = null; \}", ENTRY)
    # hash and query carried over, no history entry, no flash (nothing but a plain placeholder rendered)
    assert "location.search" in ENTRY and "location.hash" in ENTRY
    assert "location.replace(target)" in ENTRY
    assert "<noscript>" in ENTRY  # still reachable without script
    assert not re.search(r'(?:src|href)="https?://', ENTRY)


def test_key_and_values_match_the_pages():
    assert "var PREF_KEY = 'climate_advisor_dashboard'" in CORE
    assert "ND.ls.set(PREF_KEY, 'new')" in CORE and "ND.ls.set(PREF_KEY, 'classic')" in CORE
    assert 'id="back-classic"' in NEW and 'href="index.html"' in NEW


def test_classic_header_link_is_above_the_tabs_so_every_tab_shows_it():
    link = CLASSIC.index('id="new-dashboard-link"')
    assert link < CLASSIC.index('<div class="tabs">')
    assert CLASSIC.count("new_dashboard.html") == 1
    assert "</h1>" in CLASSIC[link : link + 300]


def test_panel_opens_entry_and_both_dashboards_are_served():
    zones, fake_hass, _ = build_headless_multi_zone(zone_count=1)
    assert len(zones) == 1
    url = fake_hass.data["_panels"][PANEL_FRONTEND_PATH]["config"]["url"]
    assert re.fullmatch(rf"{re.escape(PANEL_URL)}/entry\.html\?v=[0-9a-f]{{8}}", url)
    # the static path is the whole frontend folder, so entry, classic and new are all served
    src = (ROOT / "custom_components" / "climate_advisor" / "__init__.py").read_text(encoding="utf-8")
    assert "StaticPathConfig(PANEL_URL, str(frontend_path), cache_headers=False)" in src
    for name in ("entry.html", "index.html", "new_dashboard.html"):
        assert (FE / name).is_file()
