# ruff: noqa: E501
"""Issue #1230 R5: static checks for the opt-in new dashboard (frontend/new_dashboard.html + nd_*.js).

Like the other frontend tests this reads the source (CI has no browser); the real-browser pass lives in
tests/browser/check_new_dashboard.py. The heart of this file is the PARITY table: every element of the classic
dashboard (scanned from index.html, plus the plan's section 7 mapping) and where it lives in the new one. A
"pending" row is a strict xfail with the reason: when the track that owns it lands its anchor, the row flips to
XPASS (a failure) and must be moved to "placed", so the table cannot go stale in either direction.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
FE = ROOT / "custom_components" / "climate_advisor" / "frontend"
HTML = (FE / "new_dashboard.html").read_text(encoding="utf-8")
JS = {p.name: p.read_text(encoding="utf-8") for p in sorted(FE.glob("nd_*.js"))}
# R6fe: the chart (and the plan list and overlay) is one shared module, chart_v2.js + chart_v2.css, loaded by BOTH pages.
CHART_JS = (FE / "chart_v2.js").read_text(encoding="utf-8")
CHART_CSS = (FE / "chart_v2.css").read_text(encoding="utf-8")
CORPUS = HTML + "\n".join(JS.values()) + "\n" + CHART_JS + "\n" + CHART_CSS
# the classic page's behaviour = its own file + the shared chart module it loads
INDEX = (FE / "index.html").read_text(encoding="utf-8") + "\n" + CHART_JS
FIX = json.loads((ROOT / "tests" / "fixtures" / "new_dashboard_api.json").read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------------------------
# Parity: classic element -> new home
# ---------------------------------------------------------------------------------------------

# (classic ids covered, classic anchor in index.html, new home description, anchors that must exist in the new dashboard, status, reason)
PARITY = [
    (
        ["chart-main-model"],
        "chart-main-model",
        "Chart key: main prediction engine and comparison (engine registry)",
        ["chart-main-model"],
        "placed",
        "",
    ),
    # ---- header / shell
    (
        ["zone-selector-row"],
        "zone-selector-row",
        "Header zone selector",
        ['id="zone-select"', "renderZones"],
        "placed",
        "",
    ),
    (["tab-status"], "tab-status", "Status tab (shell, banners, slots)", ['id="panel-status"'], "placed", ""),
    (["tab-ai"], "tab-ai", "Analysis tab", ['id="panel-analysis"'], "placed", ""),
    (["tab-debug"], "tab-debug", "Debug tab", ['id="panel-debug"'], "placed", ""),
    (["tab-settings"], "tab-settings", "Settings tab", ['id="panel-settings"'], "placed", ""),
    # ---- Status: current status grid
    (
        ["status-card", "status-grid", "status-loading"],
        "Current Status",
        "Health line + banners (readings arrive as Status cards)",
        ['id="health"', "buildBanners"],
        "placed",
        "",
    ),
    (
        [],
        'id="status-grid"',
        "Status reading cards (R2 replaced the classic 'Conditions' grid; 'Today' / 'Outdoor sensor' cards)",
        ["status-card-today", "status-card-outdoor", "status-card-occupancy"],
        "placed",
        "",
    ),
    (
        [],
        'label">${escapeHtml(m.name)} · ${escapeHtml(role)}<',
        "Status 'Hallway' card (mode, action, set, CA target, #1194 offset text)",
        ["status-card-hallway"],
        "placed",
        "",
    ),
    (
        [],
        "data.automation_status",
        "Header health line carries on/off; the status sentence goes on the Hallway card",
        ['id="status-automation-line"'],
        "placed",
        "",
    ),
    (
        [],
        "data.coordinator_healthy === false",
        "Banner 'Climate Advisor is unavailable'",
        ["Climate Advisor is unavailable"],
        "placed",
        "",
    ),
    (
        [],
        "data.invariant_violations",
        "Banners 'Problem' (invariant violations)",
        ["invariant_violations"],
        "placed",
        "",
    ),
    ([], "data.hvac_control_issues", "Banners 'Problem' (HVAC control issues)", ["hvac_control_issues"], "placed", ""),
    (
        [],
        "data.entity_health_issues",
        "Banners: weather source / sensor problem",
        ["entity_health_issues", "Weather source problem", "Sensor problem: "],
        "placed",
        "",
    ),
    (
        [],
        "pause_suppressed_classification_text",
        "Paused banner text",
        ["pause_suppressed_classification_text"],
        "placed",
        "",
    ),
    (
        [],
        "nat_vent_off_threshold",
        "Nat-vent cycling range in the plan list",
        ['id="status-natvent-line"', "cycling"],
        "placed",
        "",
    ),
    (
        [],
        "sleep_indoor_sensor_active",
        "Sleep sensor note on the Bedroom card",
        ['id="status-sleep-sensor"'],
        "placed",
        "",
    ),
    (
        [],
        'label">Occupancy · Comfort<',
        "Vacation/away banner; Occupancy + comfort card",
        ["Vacation mode"],
        "placed",
        "",
    ),
    ([], "<td>Comfort Score</td>", "Analysis headline 'Comfort score'", ["Comfort score"], "placed", ""),
    ([], "Next User Action", "Status 'For you'", ['id="status-for-you"', "next_action"], "placed", ""),
    ([], "Next Automation", "Status 'CA next'", ['id="status-ca-next"', "next_automation_action"], "placed", ""),
    (
        [],
        "Automation Time",
        "Status 'CA next' time",
        ['id="status-ca-next-time"', "next_automation_time"],
        "placed",
        "",
    ),
    (
        [],
        "Fan (WHF)",
        "Doors & windows card fan note; fan override banner",
        ["status-card-doors", "Fan (WHF)"],
        "placed",
        "",
    ),
    (
        [],
        "fan_off_hold_active",
        "Fan override / fan-off pause banner with its button",
        ["Fan override is active", "End fan-off pause"],
        "placed",
        "",
    ),
    ([], "Doors/Windows", "Status 'Doors & windows' card", ["status-card-doors"], "placed", ""),
    ([], "not responding", "Unreadable contact sensor banner", ["Sensor problem: ", "unreadable"], "placed", ""),
    # ---- Status: briefing
    (
        ["briefing-loading", "briefing-text", "briefing-action-result"],
        "Today's Strategy",
        "Collapsed briefing panel on Status",
        ['id="status-briefing"', "status-briefing-text"],
        "placed",
        "",
    ),
    (
        [],
        "doAction('send_briefing', {notify: false})",
        "Briefing panel Regenerate",
        ['id="status-briefing-regenerate"', "notify: false"],
        "placed",
        "",
    ),
    # ---- Status: chart
    (
        ["temp-chart"],
        "temp-chart",
        "Temperature chart",
        ['id="status-chart"', 'id="temp-chart"', "CAChartV2.init"],
        "placed",
        "",
    ),
    (
        [
            "chart-card",
            "chart-cursor",
            "chart-empty",
            "chart-key",
            "chart-key-body",
            "chart-key-count",
            "chart-nav-now",
            "chart-stack",
            "chart-table-copy",
            "chart-table-toggle",
            "chart-table-wrap",
            "chart-title",
            "chart-v4-note",
            "out-chart",
        ],
        "chart-stack",
        "Chart v2 parts (R2): split indoor/outdoor charts, key, Now, table view, v4 gate note, empty state",
        ['id="status-chart"', 'id="temp-chart"', "CAChartV2.init"],
        "placed",
        "",
    ),
    (
        ["status-next"],
        "status-next",
        "Status 'what to do / CA next' row (R2 cards)",
        ['id="status-plan-list"', 'id="status-for-you"'],
        "placed",
        "",
    ),
    (
        ["activity-chart"],
        "activity-chart",
        "Activity strips under the chart",
        ['id="status-chart-strips"', 'id="activity-chart"'],
        "placed",
        "",
    ),
    (
        ["chart-zoom-reset"],
        "chart-zoom-reset",
        "Chart reset zoom",
        ['id="chart-zoom-reset"', "resetChartZoom"],
        "placed",
        "",
    ),
    (
        ["chart-nav-back", "chart-nav-fwd"],
        "chart-nav-back",
        "Chart scroll back / forward",
        ['id="status-chart-nav"', 'id="chart-nav-back"', 'id="chart-nav-fwd"'],
        "placed",
        "",
    ),
    ([], 'data-range="6h"', "Chart range 6h..1y", ['id="status-chart-range"', 'data-range="1y"'], "placed", ""),
    (
        ["chart-hover-panel"],
        "chart-hover-panel",
        "Chart hover panel",
        ['id="chart-hover-panel"'],
        "placed",
        "",
    ),
    (
        ["legend-thermostat-set"],
        "legend-thermostat-set",
        "Chart legend (compact key)",
        ['id="chart-key"', 'id="legend-thermostat-set"'],
        "placed",
        "",
    ),
    (
        [],
        "_group('Overlays', 'Overlays', overlays)",
        "Chart overlays (target band, humidity, feels-like, target)",
        ["_group('Overlays', 'Overlays', overlays)", "ov:plan"],
        "placed",
        "",
    ),
    (
        ["plan-card", "plan-foot", "plan-list", "plan-sub"],
        "plan-card",
        "Status 'Next 24 hours' plan list (R3 timeline; classic gained it in R6fe, the new Status has the same list)",
        ['id="status-plan-list"', "renderPlanList", "may help"],
        "placed",
        "",
    ),
    # ---- Status: thermal model + actions
    (
        ["thermal-model-panel", "thermal-model-content"],
        "thermal-model-panel",
        "Analysis 'Your house, as CA has learned it'",
        ['id="an-model"', "learning_health"],
        "placed",
        "",
    ),
    (["action-result"], "action-result", "Toast after every action", ['id="toast"', "ND.toast"], "placed", ""),
    (
        [],
        "Force Reclassify",
        "Actions menu: Re-plan (reclassify now)",
        ['data-action="force_reclassify"'],
        "placed",
        "",
    ),
    ([], "Send Briefing", "Actions menu: Send briefing", ['data-action="send_briefing"'], "placed", ""),
    (
        [],
        "forceUpdate()",
        "Force Update is the same force_reclassify call; one Actions item covers both",
        ['data-action="force_reclassify"'],
        "placed",
        "",
    ),
    (
        ["toggle-automation-btn"],
        "toggle-automation-btn",
        "Actions menu: Pause all automation / Resume automation (and the automation-off banner)",
        ['data-action="toggle_automation"', "Turn automation on"],
        "placed",
        "",
    ),
    (
        ["cancel-override-btn"],
        "cancel-override-btn",
        "Override / grace banner button (Cancel override / End grace period)",
        ["cancel_override", "End grace period"],
        "placed",
        "",
    ),
    (
        ["cancel-fan-override-btn"],
        "cancel-fan-override-btn",
        "Fan banner button (Cancel fan override / End fan-off pause)",
        ["cancel_fan_override"],
        "placed",
        "",
    ),
    (
        ["resume-pause-btn"],
        "resume-pause-btn",
        "Paused banner button (Resume from pause)",
        ["resume_from_pause"],
        "placed",
        "",
    ),
    # ---- Debug
    (
        ["automation-state"],
        "automation-state",
        "Debug groups (every automation_state field once, in a named group; no catch-all, a test guards new keys)",
        ['id="dbg-groups"', "DOWNLOAD_ONLY"],
        "placed",
        "",
    ),
    ([], "Door/Window Sensors", "Debug group 'Doors, windows & pause'", ["door_window_sensors"], "placed", ""),
    ([], "Fan State", "Debug group 'Fan'", ["fan_mode_config"], "placed", ""),
    ([], "Classification Details", "Debug group 'Today's classification'", ["classification"], "placed", ""),
    (
        ["daily-record", "yesterday-record", "today-record", "tomorrow-plan"],
        "daily-record",
        "Analysis days table (yesterday / today / tomorrow plan)",
        ['id="an-days"', "Tomorrow (plan)"],
        "placed",
        "",
    ),
    (
        ["engine-status"],
        "engine-status",
        "Debug 'Prediction engines' (raw) + Analysis (plain language)",
        ["k_active_hvac", "physics"],
        "placed",
        "",
    ),
    (
        [],
        "Compliance Summary",
        "Analysis days-table footnote and headline numbers",
        ['id="an-compliance"'],
        "placed",
        "",
    ),
    (
        [],
        "Household Promises Missed",
        "Analysis 'Household promises missed' + Status banner",
        ['id="an-promises"'],
        "placed",
        "",
    ),
    (
        [],
        "downloadDebugLogs()",
        "Debug header: Download logs",
        ['id="dbg-dl-logs"', "event_log?hours=168"],
        "placed",
        "",
    ),
    # ---- Settings
    (
        ["settings-content"],
        "settings-content",
        "Settings groups (read-only, with descriptions, search, jump links)",
        ['id="set-groups"', "'Other'"],
        "placed",
        "",
    ),
    (
        [],
        "Reconfigure in Home Assistant",
        "Settings: Change in Home Assistant button",
        ['id="set-ha"', "/config/integrations/integration/climate_advisor"],
        "placed",
        "",
    ),
    # ---- Analysis
    (
        ["ai-status-loading", "ai-status-content", "ai-status-grid"],
        "ai-status-grid",
        "Analysis Reports AI line + Settings AI status",
        ['id="an-ai-line"', "set-int-body"],
        "placed",
        "",
    ),
    (
        ["prediction-accuracy-link"],
        "prediction-accuracy-link",
        "Analysis 'Prediction accuracy' (R1's page embedded)",
        ['id="pa-frame"', 'id="prediction-accuracy-link"'],
        "placed",
        "",
    ),
    (
        ["suggestions-container"],
        "suggestions-container",
        "Analysis Suggestions",
        ['id="an-suggestions"', "respond_suggestion"],
        "placed",
        "",
    ),
    (
        ["report-type", "report-time-window", "run-report-btn"],
        "report-type",
        "Analysis Reports: type, window, Generate",
        ['id="report-type"', 'id="run-report-btn"'],
        "placed",
        "",
    ),
    (
        ["investigation-focus", "investigation-focus-controls", "investigation-deep"],
        "investigation-focus",
        "Analysis Reports: focus text and Deep checkbox",
        ['id="investigation-focus"', 'id="investigation-deep"'],
        "placed",
        "",
    ),
    (
        ["ai-disabled-notice"],
        "ai-disabled-notice",
        "Analysis Reports: AI-not-configured notice",
        ['id="ai-disabled-notice"'],
        "placed",
        "",
    ),
    (
        ["report-status", "report-loading", "report-content", "report-actions"],
        "report-actions",
        "Analysis Reports: result, Copy, Download .md, Full logs",
        ['id="rep-copy"', 'id="rep-download"', 'id="rep-fulllogs"'],
        "placed",
        "",
    ),
    (
        [
            "github-issue-modal",
            "github-issue-title",
            "github-issue-body",
            "github-issue-labels",
            "github-issue-status",
            "github-modal-not-configured",
            "github-submit-btn",
        ],
        "github-issue-modal",
        "GitHub issue dialog (same fields and submit call)",
        ['id="gh-modal"', "submit_github_issue"],
        "placed",
        "",
    ),
    (
        ["hist-filter-all", "hist-filter-investigation", "hist-from-date", "hist-to-date", "unified-history-list"],
        "unified-history-list",
        "Analysis report history (filters, date range, open/copy/issue/delete)",
        ['id="hist-filter-all"', "delete_report"],
        "placed",
        "",
    ),
]

# Classic container ids that are structure only (no behaviour of their own beyond what a row above covers).
STRUCTURE_IDS: set[str] = {"new-dashboard-link"}  # the one link classic gets to this page


def _classic_static_ids() -> set[str]:
    return {i for i in re.findall(r'\bid="([^"$]+)"', INDEX) if "${" not in i}


def test_every_classic_id_is_in_the_parity_table():
    covered = {i for row in PARITY for i in row[0]} | STRUCTURE_IDS
    missing = sorted(_classic_static_ids() - covered)
    assert not missing, f"classic ids with no parity row (add one): {missing}"


def test_parity_rows_name_real_classic_elements():
    for ids, anchor, *_ in PARITY:
        assert anchor in INDEX, f"classic anchor not found in index.html: {anchor!r}"
        for i in ids:
            assert f'id="{i}"' in INDEX, i


def _row_param(row):
    ids, anchor, home, anchors, status, reason = row
    marks = []
    if status == "pending":
        assert reason, f"pending row {anchor!r} needs a reason"
        marks.append(pytest.mark.xfail(reason=reason, strict=True))
    return pytest.param(row, id=f"{status}:{anchor[:40]}", marks=marks)


@pytest.mark.parametrize("row", [_row_param(r) for r in PARITY])
def test_parity_row(row):
    _ids, _anchor, home, anchors, status, _reason = row
    for a in anchors:
        assert a in CORPUS, f"{home}: expected {a!r} in the new dashboard"


def test_parity_summary():
    placed = [r for r in PARITY if r[4] == "placed"]
    pending = [r for r in PARITY if r[4] == "pending"]
    assert len(placed) + len(pending) == len(PARITY)
    assert len(placed) >= 40
    # R6fe: every Status row is placed with a real anchor; nothing is pending.
    assert not pending, [r[1] for r in pending]
    assert all(
        (
            "Status" in r[2]
            or "chart" in r[2].lower()
            or "plan list" in r[2].lower()
            or "For you" in r[2]
            or "CA next" in r[2]
            or "briefing" in r[2].lower()
            or "Chart" in r[2]
            or "card" in r[2].lower()
        )
        for r in pending
    )


# ---------------------------------------------------------------------------------------------
# Shell
# ---------------------------------------------------------------------------------------------
def test_tabs_are_an_accessible_tablist_in_the_planned_order():
    assert 'role="tablist"' in HTML
    order = re.findall(r'role="tab" id="tab-btn-(\w+)"', HTML)
    assert order == ["status", "analysis", "debug", "settings"]
    for t in order:
        assert re.search(rf'id="tab-btn-{t}"[^>]*aria-controls="panel-{t}"', HTML)
        assert re.search(rf'id="panel-{t}" class="panel-tab" role="tabpanel" aria-labelledby="tab-btn-{t}"', HTML)
    core = JS["nd_core.js"]
    for key in ("ArrowRight", "ArrowLeft", "Home", "End", "aria-selected"):
        assert key in core
    assert "hashchange" in core and "history.replaceState" in core  # deep links: #analysis, #settings/comfort


def test_header_has_health_actions_zone_and_back_to_classic():
    for frag in (
        'id="zone-select"',
        'id="health"',
        'id="actions-btn"',
        'aria-haspopup="menu"',
        'role="menu"',
        'id="back-classic"',
        'href="index.html"',
    ):
        assert frag in HTML, frag
    core = JS["nd_core.js"]
    assert "climate_advisor_dashboard" in core and "'new'" in core and "'classic'" in core
    # remembered with try/catch (private mode), classic stays the default (this page never redirects classic users)
    assert re.search(r"set: function \(k, v\) \{ try \{ localStorage\.setItem", core)
    assert "location.replace" not in core and "location.href =" not in core


def test_actions_are_the_classic_posts():
    for ep in (
        "force_reclassify",
        "send_briefing",
        "toggle_automation",
        "cancel_override",
        "cancel_fan_override",
        "resume_from_pause",
    ):
        assert ep in CORPUS and ep in INDEX, ep
    core = JS["nd_core.js"]
    assert "ND.post = function (path, body)" in core and "method: 'POST'" in core
    # context buttons live only inside banners
    for act in ("cancel_override", "cancel_fan_override", "resume_from_pause"):
        assert f"act: '{act}'" in core


@pytest.mark.parametrize(
    "text",
    [
        "Automation is off",  # automation off / observe-only
        "observe-only",
        "Vacation mode",
        "Weather source problem",
        "Sensor problem: ",
        "Saved settings are waiting for a reload",
        "Paused: a door or window is open",
        "You changed the thermostat",
        "household promise",
    ],
)
def test_context_banners(text):
    assert text in JS["nd_core.js"]


def test_banner_level_roles_and_status_only_promises():
    core = JS["nd_core.js"]
    assert "'alert'" in core and "'status'" in core
    assert "['status'], 'promises'" in core  # the promise banner is Status-only


def test_auth_and_zone_handling_matches_classic():
    core = JS["nd_core.js"]
    for frag in (
        "hassTokens",
        "refreshAccessToken",
        "Bearer ",
        "HTTP 401",
        "climate_advisor_selected_zone",
        "entry_id=",
        "zone_selection_required",
        "HTTP 503",
    ):
        assert frag in core, frag
        assert frag in INDEX, frag


def test_no_external_resources_and_no_chart_library():
    assert not re.search(r'(?:src|href)="https?://', HTML)
    # R6fe: Status carries R2's chart (the bundled Chart.js, hammer and zoom plugin; no CDN); the Night card stays plain SVG
    assert "chart.umd" not in JS["nd_night_card.js"] and "Chart(" not in JS["nd_night_card.js"]
    for lib in ("hammer.min.js", "chart.umd.min.js", "chartjs-plugin-zoom.min.js"):
        assert f'<script src="{lib}"></script>' in HTML
    for name in re.findall(r'<script src="([^"]+)"', HTML):
        assert (FE / name).exists(), name


def test_classic_has_exactly_one_new_link_in_its_header():
    """R6a: one "Try the new dashboard" link, visible on every classic tab (above the tab bar). Any other
    reference is a Settings deep link (the chart key's "change it in Settings" link from the engine registry)."""
    try_links = INDEX.count('href="new_dashboard.html"')
    assert try_links == 1
    assert INDEX.index('href="new_dashboard.html"') < INDEX.index('<div class="tabs">')
    others = INDEX.count("new_dashboard.html") - try_links
    assert others == INDEX.count("new_dashboard.html#settings")


# ---------------------------------------------------------------------------------------------
# Data calls per tab
# ---------------------------------------------------------------------------------------------
def _known_endpoints() -> set[str]:
    const = (ROOT / "custom_components" / "climate_advisor" / "const.py").read_text(encoding="utf-8")
    api = (ROOT / "custom_components" / "climate_advisor" / "api.py").read_text(encoding="utf-8")
    dash = (ROOT / "custom_components" / "climate_advisor" / "dashboard_api.py").read_text(encoding="utf-8")
    urls = set(re.findall(r'"/api/climate_advisor/([a-z_]+)"', const + api + dash))
    urls |= set(re.findall(r'f"\{API_BASE\}/([a-z_]+)"', const))
    return urls


def _endpoints_used(js: str) -> set[str]:
    found = set(re.findall(r"ND\.(?:api|post|stream)\('([a-z_]+)", js))
    found |= set(re.findall(r"withZone\('([a-z_]+)'", js))
    return found


def test_every_endpoint_called_exists():
    known = _known_endpoints()
    for name, js in JS.items():
        for ep in _endpoints_used(js):
            assert ep in known, f"{name} calls unknown endpoint {ep}"


def test_status_banners_poll_status_and_settings_extra():
    assert _endpoints_used(JS["nd_core.js"]) >= {"status", "settings_extra"}


def test_analysis_data_calls_and_writes_match_classic():
    js = JS["nd_analysis.js"]
    assert _endpoints_used(js) >= {
        "learning",
        "engines",
        "event_log",
        "chart_data",
        "ai_status",
        "investigation_reports",
        "activity_record",
        "ai_investigate",
        "delete_report",
        "submit_github_issue",
        "respond_suggestion",
    }
    # bodies identical to classic
    assert (
        "JSON.stringify({ action: action, suggestion_key: key })" in js
        and "{ action: action, suggestion_key: key }".replace(" ", "") in INDEX.replace(" ", "")
    )
    assert (
        "JSON.stringify({ feedback: verdict, suggestion_key: key })" in js
        and "feedback:verdict,suggestion_key:key" in INDEX.replace(" ", "")
    )
    assert (
        "report_type: e.report_type || 'activity', timestamp: e.timestamp" in js
        and "report_type:entry.report_type||'activity',timestamp:entry.timestamp" in INDEX.replace(" ", "")
    )
    assert (
        "JSON.stringify({ title: title, body: body, labels: labels })" in js
        and "JSON.stringify({title,body,labels})" in INDEX.replace(" ", "")
    )
    assert "event_log?hours=168" in js and "activity_record?hours=" in js
    for f in ("hours", "focus", "deep"):
        assert f in js
    assert "'incident_detected'" in js  # the incident package, as classic
    # report window options are classic's
    for opt in ('value="6">Last 6 hours', 'value="168">Last 7 days', 'value="720">Last 30 days'):
        assert opt in js
    for h in ("Accept", "Dismiss", "Correct", "Incorrect"):
        assert h in js


def test_analysis_sections_exist():
    for frag in (
        "an-kpis",
        "an-days",
        "an-model",
        "an-cq",
        "an-pe",
        "an-promises",
        "an-suggestions",
        "an-rep-h",
        "an-pa-h",
        "an-ai-line",
        "Tomorrow (plan)",
    ):
        assert frag in HTML or frag in JS["nd_analysis.js"], frag
    js = JS["nd_analysis.js"]
    for tile in (
        "Comfort score",
        "Window advice followed",
        "Runtime per day",
        "Manual changes",
        "Promises missed",
        "Prediction error, 4 h",
    ):
        assert tile in js, tile
    assert "fToDisp" in js and "dFToDisp" in js  # records are internal degF; shown in the install's unit
    assert "prediction_accuracy.html" in js and "?zone=" in js  # R1's page, reused


def test_days_table_tomorrow_blank_until_the_plan_provides_it():
    js = JS["nd_analysis.js"]
    # every Tomorrow producer returns '' (blank) when it has nothing, never a made-up value
    assert js.count("function () { return ''; }") >= 7
    assert "needs an energy rate" not in js and "unknown (no occupancy schedule)" not in js


def test_debug_data_calls_and_features():
    js = JS["nd_debug.js"]
    assert _endpoints_used(js) >= {
        "automation_state",
        "engines",
        "event_log",
        "prediction_accuracy",
        "investigation_reports",
    }
    for frag in (
        "dbg-search",
        "dbg-ev-types",
        "dbg-ev-all",
        "dbg-ev-none",
        "dbg-dl-logs",
        "dbg-dl-diag",
        "dbg-bt",
        "event_log?hours=168",
        "Backtest lab",
    ):
        assert frag in js or frag in HTML, frag
    assert "ND.post" not in js and "method:" not in js  # Debug is read-only


def _debug_lists() -> tuple[set[str], set[str], set[str], set[str]]:
    """(placed in a named GROUPS list, NESTED, MOVED, DOWNLOAD_ONLY) as written in nd_debug.js."""
    js = JS["nd_debug.js"]
    block = js[js.index("var GROUPS = [") : js.index("var NESTED")]

    def _keys(name: str) -> set[str]:
        m = re.search(r"var " + name + r" = ([\[{][^;]*);", js)
        assert m, f"nd_debug.js has no {name}"
        body = m.group(1)
        if body.startswith("["):  # a list of quoted keys
            return set(re.findall(r"'([a-z_0-9]+)'", body))
        return set(re.findall(r"([a-z_0-9]+)\s*:", body))  # an object: its keys

    return set(re.findall(r"'([a-z_0-9]+)'", block)), _keys("NESTED"), _keys("MOVED"), _keys("DOWNLOAD_ONLY")


def _get_debug_state_keys() -> set[str]:
    """Every top-level key of coordinator.get_debug_state()'s returned dict, read from the source (no HA needed)."""
    import ast

    src = (ROOT / "custom_components" / "climate_advisor" / "coordinator.py").read_text(encoding="utf-8")
    fn = next(n for n in ast.walk(ast.parse(src)) if isinstance(n, ast.FunctionDef) and n.name == "get_debug_state")
    ret = [n for n in ast.walk(fn) if isinstance(n, ast.Return) and isinstance(n.value, ast.Dict)][-1]
    keys = {k.value for k in ret.value.keys if isinstance(k, ast.Constant)}
    assert len(keys) == len(ret.value.keys), "get_debug_state() returns a non-literal key"
    return keys


def test_every_debug_state_key_is_in_a_named_group_or_download_only():
    """Issue #1399: the Debug tab has no on-screen catch-all ("Other"). Instead every key get_debug_state() returns is
    placed in a named Debug group (or expanded/moved), or listed in DOWNLOAD_ONLY (kept only in Download diagnostics).
    A key added to the backend that is in none of these lists fails here, so a field can never silently vanish."""
    placed, nested, moved, download_only = _debug_lists()
    assert nested == {"classification", "thermal_pipeline"} and moved == {"promise_summary"}
    keys = _get_debug_state_keys()
    missing = sorted(keys - (placed | nested | moved | download_only))
    assert not missing, f"get_debug_state() keys in no Debug group and not in DOWNLOAD_ONLY: {missing}"
    assert not download_only & (placed | nested | moved), "a key is both shown and download-only"
    stale = sorted((placed | download_only) - keys)
    assert not stale, f"nd_debug.js names keys get_debug_state() no longer returns: {stale}"


def test_debug_tab_has_no_other_group_and_no_raw_json():
    js = JS["nd_debug.js"]
    assert "'Other'" not in js and "title: 'Other'" not in js
    i = HTML.index('id="panel-debug"')
    panel = HTML[i : HTML.index('id="panel-settings"')]
    assert "Other" not in panel and "Download diagnostics" in panel
    # JSON.stringify only builds the download files and the event search haystack, never a displayed value
    for line in (ln for ln in js.splitlines() if "JSON.stringify" in ln):
        assert "ND.download(" in line or "indexOf(q)" in line, line.strip()
    # the Debug download saves the whole automation_state payload, so every key (download-only included) is in it
    dl = js[js.index("async function downloadDiagnostics") : js.index("// ---------- backtest lab")]
    assert "automation_state: A" in dl


def test_debug_groups_cover_a_real_automation_state():
    js = JS["nd_debug.js"]
    block = js[js.index("var GROUPS = [") : js.index("var NESTED")]
    placed, nested, moved, download_only = _debug_lists()
    for key in FIX["automation_state"]:
        assert key in placed | nested | moved | download_only, f"{key} is in no Debug group (add it to one)"
    # a key is placed in at most one group
    seen = []
    for m in re.finditer(r"\['[^']+', \[([^\]]*)\]\]", block):
        seen += re.findall(r"'([a-z_0-9]+)'", m.group(1))
    assert len(seen) == len(set(seen)), "a Debug field appears in two groups"


def test_settings_is_read_only_and_uses_the_same_config_call():
    js = JS["nd_settings.js"]
    assert _endpoints_used(js) >= {"config", "settings_extra", "ai_status"}
    assert "ND.post" not in js and "method:" not in js and "fetch(" not in js
    assert "/config/integrations/integration/climate_advisor" in HTML
    assert "read-only" in js and "read-only" in HTML
    for frag in (
        "set-search",
        "set-desc",
        "set-changed",
        "set-jump",
        "set-tou",
        "set-int-body",
        "changed from default",
    ):
        assert frag in HTML or frag in js, frag


def test_settings_groups_use_real_keys_and_leave_few_in_other():
    from custom_components.climate_advisor.const import CONFIG_METADATA

    js = JS["nd_settings.js"]
    block = js[js.index("var G = [") : js.index("function fmt(")]
    keys = re.findall(r"'([a-z_0-9]+)'", block)
    names = {"comfort", "equipment", "sensors", "occupancy", "timing", "notify", "learning", "daytypes", "ai"}
    keys = [
        k
        for k in keys
        if k not in names and k in CONFIG_METADATA or (k not in names and k not in CONFIG_METADATA and "_" in k)
    ]
    unknown = [k for k in keys if k not in CONFIG_METADATA]
    assert not unknown, f"settings groups name keys that do not exist: {unknown}"
    assert len(keys) == len(set(keys)), "a setting is in two groups"
    unplaced = [k for k in CONFIG_METADATA if k not in keys]
    assert len(unplaced) <= 3, f"{len(unplaced)} settings would land in Other: {unplaced}"


# ---------------------------------------------------------------------------------------------
# Night card (Status mounts it; R4 supplies live data)
# ---------------------------------------------------------------------------------------------
def test_night_card_is_a_self_contained_component():
    card = JS["nd_night_card.js"]
    assert "window.NDNightCard = { mount: mount, fromStrategy: fromStrategy }" in card
    assert "fetch(" not in card and "ND." not in card  # no network, no dependency on the page's globals
    for frag in (
        "Tonight",
        "RECOMMENDED FOR",
        "aria-pressed",
        'role="img"',
        "sleep band",
        "Not validated yet",
        "fahrenheit",
        "celsius",
    ):
        assert frag in card or frag.lower() in card.lower(), frag
    status = JS["nd_status.js"]
    assert "NDNightCard.mount" in status and "NDNightCard.fromStrategy" in status
    assert "Sample data, not your house" in HTML  # the labelled fallback for an install without the endpoint
    sample = json.loads((FE / "nd_night_sample.json").read_text(encoding="utf-8"))
    assert sample["nights"] and all({"t", "out", "floor", "ceiling", "rows"} <= set(n) for n in sample["nights"])
    for n in sample["nights"]:
        for r in n["rows"]:
            assert {"key", "label", "bed", "bed_min", "bed_max", "bed_wake", "heat_min", "cool_min", "cost"} <= set(r)
            assert len(r["bed"]) == len(n["t"])


def test_status_tab_is_assembled_from_the_shared_parts():
    status = JS["nd_status.js"]
    # the placeholder slots are gone: every part is real
    for gone in ('data-slot="chart"', 'data-slot="plan"', "arrives with", "coming)"):
        assert gone not in HTML, gone
    # order on the page: for you / CA next, readings, chart, plan list, night card, briefing
    order = [
        'id="status-for-you"',
        'id="status-grid"',
        'id="status-chart"',
        'id="status-plan-list"',
        'id="night-card"',
        'id="status-briefing"',
    ]
    at = [HTML.index(o) for o in order]
    assert at == sorted(at)
    # the chart is the shared module: one implementation for both pages, no copy of its code here
    assert '<script src="chart_v2.js"></script>' in HTML and '<script src="chart_v2.js"></script>' in (
        FE / "index.html"
    ).read_text(encoding="utf-8")
    assert '<link rel="stylesheet" href="chart_v2.css">' in HTML
    assert "new Chart(" not in status and "new Chart(" not in HTML and "new Chart(" in CHART_JS
    assert "CAChartV2.init" in status and "CAChartV2.mountPlan" in status and "CAChartV2.loadTimeline" in status
    # the readings and the next lines come from the status payload (the timeline is authoritative for them since R3b)
    for key in (
        "next_action",
        "next_automation_action",
        "next_automation_time",
        "automation_status",
        "sleep_indoor_sensor_active",
    ):
        assert key in status
    # the briefing: text, Regenerate (no notification) and Send, the classic calls
    assert "ND.api('briefing')" in status
    assert "ND.runAction('send_briefing', { notify: false }" in status
    assert "ND.runAction('send_briefing', undefined" in status


def test_status_tab_reads_the_live_night_plan_and_gates_its_figures():
    status, card = JS["nd_status.js"], JS["nd_night_card.js"]
    assert "ND.api('night_strategy')" in status
    # empty state on 503 (no plan yet), the labelled sample only for a missing endpoint (404), the badge off when live
    assert "e.status === 503" in status and "e.status === 404" in status
    assert "ND.$('night-sample-badge').hidden = mode !== 'sample'" in status
    assert 'id="night-sample-badge"' in HTML and "hidden" in re.search(r'id="night-sample-badge"[^>]*', HTML).group(0)
    # show_numbers false: the plan and its steps, no predicted figures (decision C11)
    assert "if (data.show_numbers === false ||" in card and "night-nonumbers" in card
    plan_only = card[card.index("// plan only: no predicted figures") : card.index("var W = 1000")]
    for figure in ("bed_min", "bed_max", "bed_wake", "d1(", "heat_min", "<svg", "<table"):
        assert figure not in plan_only, f"plan-only view must not show {figure}"
    # low-confidence steps keep the qualifier (decision C13)
    assert "may help" in card


def test_status_places_the_same_actors_as_the_timeline():
    mod = CHART_JS
    # "you" and "CA" are named in words and by shape (never colour alone), low confidence says "may help"
    assert "'You'" in mod and "'CA'" in mod and "may help" in mod
    assert "s.confidence === 'low'" in mod
    # nothing in the list invents wording: the step's own text is what is shown
    assert "escapeHtml(s.text || '')" in mod


# ---------------------------------------------------------------------------------------------
# Accessibility, theming, contrast
# ---------------------------------------------------------------------------------------------
def test_every_form_control_is_labelled():
    for m in re.finditer(r"<(input|select|textarea)\b([^>]*)>", HTML):
        attrs = m.group(2)
        cid = re.search(r'id="([^"]+)"', attrs)
        labelled = "aria-label" in attrs or (cid and (f'for="{cid.group(1)}"' in HTML))
        if not labelled:  # wrapped by a <label> element
            before = HTML[: m.start()]
            labelled = before.rfind("<label") > before.rfind("</label>")
        assert labelled, f"unlabelled control: {m.group(0)[:80]}"


def test_focus_visible_and_reduced_surprises():
    assert ":focus-visible" in HTML
    assert 'lang="en"' in HTML and 'name="viewport"' in HTML
    assert (
        "overflow-x: hidden" in HTML
    )  # phone: the page itself never scrolls sideways (tables scroll inside .scroll-x)
    assert "scroll-x" in HTML


def _tokens(block: str) -> dict[str, str]:
    return dict(re.findall(r"--([a-z0-9-]+):\s*(#[0-9a-fA-F]{6})", block))


def _lum(hex_: str) -> float:
    r, g, b = (int(hex_[i : i + 2], 16) / 255 for i in (1, 3, 5))
    f = lambda v: v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4  # noqa: E731
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)


def _ratio(a: str, b: str) -> float:
    la, lb = _lum(a), _lum(b)
    return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)


def test_light_and_dark_token_pairs_meet_4_5_to_1():
    light = _tokens(HTML[HTML.index(":root {") : HTML.index("@media (prefers-color-scheme: dark)")])
    dark_start = HTML.index(':root:where(:not([data-theme="light"]))')
    dark = _tokens(HTML[dark_start : HTML.index(':root[data-theme="dark"]')])
    forced = _tokens(HTML[HTML.index(':root[data-theme="dark"]') : HTML.index("* { box-sizing")])
    assert dark == forced  # the manual dark override and the media query agree
    pairs = [
        ("ink", "bg"),
        ("ink", "panel"),
        ("ink2", "bg"),
        ("ink2", "panel"),
        ("ink2", "chip"),
        ("muted", "bg"),
        ("muted", "panel"),
        ("muted", "chip"),
        ("s1", "panel"),
        ("s1", "bg"),
        ("on-s1", "s1"),
        ("warn", "warnbg"),
        ("ok", "okbg"),
        ("bad", "badbg"),
        ("ink", "infobg"),
        ("warn", "panel"),
        ("bad", "panel"),
        ("s2", "panel"),
        ("ink", "warnbg"),
        ("ink", "badbg"),
    ]
    for name, toks in (("light", light), ("dark", dark)):
        for fg, bg in pairs:
            assert _ratio(toks[fg], toks[bg]) >= 4.5, f"{name}: {fg} on {bg} = {_ratio(toks[fg], toks[bg]):.2f}"


def test_both_color_schemes_and_units_are_handled():
    assert "prefers-color-scheme: dark" in HTML and 'data-theme="dark"' in HTML
    assert "isC" in JS["nd_core.js"] and "celsius" in JS["nd_core.js"]
    assert "unit" in JS["nd_night_card.js"]
