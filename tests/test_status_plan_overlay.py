"""Issue #1230 R6fe: static checks for the shared chart module, the plan list and the chart plan overlay.

No JS runtime here: these pin what the page code reads and refuses to do. Behaviour is in tests/ui/plan.spec.js (real
Chromium) and tests/browser/check_status_final.py (both pages, themes, widths, every control).
"""

from __future__ import annotations

import re
from pathlib import Path

FE = Path(__file__).resolve().parent.parent / "custom_components" / "climate_advisor" / "frontend"
MOD = (FE / "chart_v2.js").read_text(encoding="utf-8")
CSS = (FE / "chart_v2.css").read_text(encoding="utf-8")
CLASSIC = (FE / "index.html").read_text(encoding="utf-8")
NEW = (FE / "new_dashboard.html").read_text(encoding="utf-8")
STATUS_JS = (FE / "nd_status.js").read_text(encoding="utf-8")


def _block(name_start: str, name_end: str) -> str:
    return MOD[MOD.index(name_start) : MOD.index(name_end)]


PLAN = _block("// ---- plan: the operation timeline (R3)", "  function init(host)")


def test_one_chart_module_serves_both_pages():
    for html in (CLASSIC, NEW):
        assert '<script src="chart_v2.js"></script>' in html
        assert '<link rel="stylesheet" href="chart_v2.css">' in html
        for lib in ("hammer.min.js", "chart.umd.min.js", "chartjs-plugin-zoom.min.js"):
            assert f'<script src="{lib}"></script>' in html
    # the chart code lives in the module only: neither page builds a Chart or draws a dataset itself
    for html in (CLASSIC, NEW):
        assert "new Chart(" not in html
    assert MOD.count("new Chart(") == 2  # indoor and outdoor
    # the page adapts it through a small host object
    for key in ("zone:", "api:", "onData:"):
        assert key in CLASSIC and key in STATUS_JS
    # the chart styles moved with it (the classic page keeps none of them)
    assert ".key-chip" in CSS and ".chart-stack" in CSS and ".plan-row" in CSS
    assert ".key-chip" not in CLASSIC and ".chart-stack" not in CLASSIC


def test_classic_status_has_the_plan_list_under_the_chart():
    assert (
        CLASSIC.index('id="chart-card"') < CLASSIC.index('id="plan-card"') < CLASSIC.index('id="thermal-model-panel"')
    )
    assert 'id="plan-list"' in CLASSIC and 'aria-live="polite"' in CLASSIC
    assert "CAChartV2.mountPlan(document.getElementById('plan-list'))" in CLASSIC
    assert "function loadPlan() { return CAChartV2.loadTimeline(); }" in CLASSIC
    # refreshed with the chart (same cadence) and by every load of it
    assert re.search(r"onData: \(data\) => \{[^}]*loadPlan\(\);", CLASSIC, re.S)


def test_the_plan_is_read_from_the_timeline_endpoint_and_never_recomputed():
    assert "_host.api('timeline')" in MOD
    assert "e.status === 503" in MOD  # no plan yet is an empty state, not an error
    for state in ("'none'", "'error'", "'idle'", "'ok'"):
        assert state in PLAN
    # only the contract's fields: steps (ts, actor, kind, text, confidence, lead_minutes, meta.status) and path
    for field in (
        "s.actor",
        "s.kind",
        "s.text",
        "s.confidence",
        "s.lead_minutes",
        "s.meta",
        "_timeline.path",
        "p.temp_f",
        "p.sensor",
        "p.model",
    ):
        assert field in PLAN, field
    # no planning logic of its own: no thresholds, no band, no setpoint arithmetic on steps
    assert "target_f" not in PLAN and "setpoint" not in PLAN and "floor" not in PLAN


def test_list_window_ordering_and_hidden_kinds():
    assert "PLAN_HORIZON_MS = 24 * 3600000" in PLAN
    assert "PLAN_HIDE_KINDS = ['briefing_send']" in PLAN
    assert ".sort((a, b) => a._ms - b._ms)" in PLAN
    # steps already done are not offered again
    assert "s.meta.status === 'done'" in PLAN
    # a step is dropped ten minutes after its time
    assert "s._ms > now - 10 * 60000" in PLAN


def test_you_steps_carry_the_reminder_and_low_confidence_says_may_help():
    assert "s.actor === 'you'" in PLAN
    assert "Reminder at " in PLAN and "s.lead_minutes * 60000" in PLAN  # decision DP-4: 30 minutes ahead
    assert "s.confidence === 'low'" in PLAN
    assert PLAN.count("may help") >= 1  # decision C13
    # who does it is a word AND a shape, never colour alone
    assert "(you ? 'You' : 'CA')" in PLAN
    assert "M0 -6 L6 0 L0 6 L-6 0 Z" in PLAN and '<circle r="4.5"' in PLAN
    assert 'role="img"' not in PLAN  # the markers are decoration for the list that says the same in words


def test_the_module_adds_no_mechanism_words_to_the_plan_list():
    """Status Card Ontology (#527, #849): occupant-facing strings say what happens, not how the machine waits."""
    strings = re.findall(r"'([^'\n]{3,})'", PLAN) + re.findall(r'"([^"\n]{3,})"', PLAN)
    visible = [s for s in strings if " " in s and not s.startswith(("<", "var(", "--"))]
    banned = ("waiting", "paused", "grace", "override", "confirming")
    for s in visible:
        for word in banned:
            assert word not in s.lower(), f"{word!r} in plan wording: {s!r}"
    # empty and error states are plain
    assert "Nothing is planned for the next 24 hours." in PLAN
    assert "The plan appears after Climate Advisor has classified the day." in PLAN


def test_plan_overlay_is_one_toggle_in_the_overlays_group_remembered_with_the_others():
    overlays = MOD[MOD.index("const overlays =") : MOD.index("const stripChips")]
    assert "ovChip('ov:plan'" in overlays and "<span>Plan</span>" in overlays
    # it goes through the same toggle and storage path as every other chip (try/catch storage, per zone)
    # the stored key is per role since #1399 (_prefId), so an engine switch keeps the choice
    assert "if (id.startsWith('strip:'))" in MOD and "_chartPrefs.on[_prefId(id)] = !_isOn(id)" in MOD
    assert "plan: 'ov:plan'" in MOD  # also reachable through toggleOverlay('plan')
    # on by default: it is the plan the occupant came to read (only baseline, humidity, feels-like, CA line start off)
    default_on = MOD[MOD.index("function _defaultOn(id)") : MOD.index("function _isOn(id)")]
    assert "ov:plan" not in default_on


def test_plan_overlay_draws_markers_on_the_future_side_and_a_path_gated_like_v4():
    assert "function _drawPlanMarkers(ctx, xs, ys)" in PLAN
    assert "if (s._ms <= now) return;" in PLAN  # markers only to the right of NOW
    assert "if (isIndoor) _drawPlanMarkers(ctx, xs, ys);" in MOD
    # the plan path never shows v4 numbers for a zone whose accuracy check has not passed (decision C9/C11)
    assert "p.model !== 'v4' || v4ok" in PLAN
    assert "_lastChartData.v4.ready === true" in PLAN
    assert "caId: 'ov:plan', label: 'Plan path'" in MOD
    # hover readout names the step near the cursor
    assert "_planNear(ms)" in MOD


def test_new_status_shows_the_nat_vent_range_in_the_plan_list_not_twice():
    assert 'id="status-natvent-line"' in NEW and "natVentLine" in STATUS_JS
    assert "extra: natVentLine" in STATUS_JS
    # classic keeps it in the Status card note, so its plan list has no foot text
    assert "extra:" not in CLASSIC[CLASSIC.index("CAChartV2.mountPlan") : CLASSIC.index("CAChartV2.mountPlan") + 120]


def test_phone_layout_and_contrast_tokens_for_the_plan_list():
    assert re.search(
        r"@media \(max-width: 640px\) \{\s*\.plan-row \{ grid-template-columns: 5\.4em minmax\(0, 1fr\); \}", CSS
    )
    assert ".plan-qual" in CSS and "overflow-wrap: anywhere" in CSS
    # the new dashboard maps its own tokens onto the names the shared styles read
    for pair in (
        "--text: var(--ink)",
        "--text-secondary: var(--muted)",
        "--border: var(--rule)",
        "--card-bg: var(--panel)",
    ):
        assert pair in NEW, pair
    # an explicit theme choice wins over the system setting for the chart colours
    assert ':root[data-theme="dark"]' in CSS and ':root[data-theme="light"]' in CSS
