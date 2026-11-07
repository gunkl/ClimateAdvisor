"""Issue #1230 R1 lane D: static checks for the "Prediction accuracy (preview)" page.

The repo has no JS runtime or headless browser, so (like the other frontend tests) these read the
HTML/JS source and assert the invariants that matter: every control exists with its aria wiring,
the request URL is built from each control, empty/error states exist, units come from the
response, only bundled libraries load, and index.html links to the page.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FRONTEND = ROOT / "custom_components" / "climate_advisor" / "frontend"
PAGE = (FRONTEND / "prediction_accuracy.html").read_text(encoding="utf-8")
INDEX = (
    (FRONTEND / "index.html").read_text(encoding="utf-8")
    + "\n"
    + (FRONTEND / "chart_v2.js").read_text(encoding="utf-8")
)
FIXTURE = json.loads((ROOT / "tests" / "fixtures" / "prediction_accuracy_sample.json").read_text(encoding="utf-8"))


def _func(name: str) -> str:
    m = re.search(rf"  (?:async )?function {name}\(.*?\n  \}}\n", PAGE, re.DOTALL)
    if not m:  # one-line functions
        m = re.search(rf"  function {name}\(.*", PAGE)
    assert m, f"{name} not found"
    return m.group(0)


# ---- controls ----------------------------------------------------------------------------------


def test_control_groups_present_with_aria():
    for gid, label in (
        ("grp-sensor", 'aria-labelledby="lbl-sensor"'),
        ("grp-lead", 'aria-label="How far ahead the prediction was made"'),
        ("grp-range", 'aria-label="Time range"'),
    ):
        assert re.search(rf'role="group" {re.escape(label)} id="{gid}"', PAGE), gid
    assert 'id="reset-zoom"' in PAGE and "Reset zoom" in PAGE
    assert 'id="zone-select"' in PAGE


def test_buttons_are_real_buttons_with_aria_pressed():
    seg = _func("segButton")
    assert "createElement('button')" in seg
    assert "b.type = 'button'" in seg
    assert "aria-pressed" in seg
    assert "aria-label" in seg


def test_ranges_leads_and_scroll():
    assert "var RANGES = ['6h', '12h', '24h', '3d', '7d', '30d', '1y']" in PAGE
    assert "var LEADS = ['1h', '4h', '12h', '24h']" in PAGE
    assert "'Scroll back'" in PAGE and "'Scroll forward'" in PAGE
    assert "\u2039" in PAGE and "\u203a" in PAGE
    scroll = _func("scroll")
    assert "S.before" in scroll and "toISOString" in scroll
    assert "S.before = null" in scroll  # snaps back to live


def test_sensor_group_is_ca_reading_plus_response_sensors():
    sl = _func("sensorList")
    assert "'CA reading'" in sl
    assert "S.sensors" in sl
    assert "S.sensors = d.sensors" in PAGE
    assert "var CA_SENSOR = '_ca'" in PAGE


def test_v3_only_for_ca_reading():
    assert "S.sensor === CA_SENSOR" in _func("hasV3")
    assert 'id="legend-v3"' in PAGE
    assert "legend-v3').hidden = S.sensor !== CA_SENSOR" in PAGE


def test_zoom_and_hover_wiring():
    assert "drag: { enabled: true" in PAGE
    assert "pinch: { enabled: true }" in PAGE
    assert "resetZoom" in PAGE
    assert "onKey" in PAGE and "ArrowLeft" in PAGE and 'tabindex="0"' in PAGE
    assert 'id="hover"' in PAGE


def test_heat_strip_tiles_gate_and_seasons():
    assert "heat_runs" in PAGE and "Heat ran" in PAGE
    for t in ("v4 average miss", "v3 average miss", "v4 runs warm or cold"):
        assert t in PAGE
    assert "S.data.stats" in PAGE
    assert 'id="gate-table"' in PAGE and "Ready to show v4 on Status?" in PAGE
    assert "Beats v3" in PAGE and "Beats no change" in PAGE
    assert 'id="seasons-note"' in PAGE and "Seasons are scored apart" in PAGE
    assert "r.scored === false" in PAGE and "Not scored yet" in PAGE


# ---- request building --------------------------------------------------------------------------


def test_request_url_built_from_every_control():
    q = _func("buildQuery")
    for key in ("zone", "sensor", "lead", "range", "before"):
        assert f"p.set('{key}'" in q, key
    # zone and before are optional; sensor/lead/range are always sent
    assert "if (st.zone)" in q and "if (st.before)" in q
    assert "var API_PATH = '/api/climate_advisor/prediction_accuracy'" in PAGE
    assert "buildUrl(S)" in PAGE


def test_every_control_triggers_a_reload_with_correct_state():
    assert re.search(r"function pickSensor\(id\) \{ S\.sensor = id;.*load\(\)", PAGE)
    assert re.search(r"function pickLead\(l\) \{ S\.lead = l;.*load\(\)", PAGE)
    assert re.search(r"function pickRange\(r\) \{ S\.range = r; S\.before = null;.*load\(\)", PAGE)


def test_fixture_matches_contract_shape():
    for k in (
        "zone",
        "unit",
        "sensors",
        "lead",
        "range",
        "start",
        "end",
        "downsample",
        "points",
        "heat_runs",
        "stats",
        "model",
        "gate",
        "sensor",
        "gate_summary",
    ):
        assert k in FIXTURE, k
    assert FIXTURE["sensors"][0]["id"] == "_ca" and FIXTURE["sensor"] == "_ca"  # the endpoint lists `_ca` first
    assert {"ready", "passed", "scored", "total"} <= set(FIXTURE["gate_summary"])
    assert {"t", "actual", "v4", "v3"} <= set(FIXTURE["points"][0])
    assert {"n", "v4_mae", "v3_mae", "flat_mae", "v4_bias", "v3_bias"} <= set(FIXTURE["stats"])
    assert {"zone", "season", "lead", "v4", "v3", "flat", "bias", "pass", "scored"} <= set(FIXTURE["gate"][0])
    assert FIXTURE["stats"]["v4_mae"] < FIXTURE["stats"]["v3_mae"]


# ---- states, units, libraries, theming ---------------------------------------------------------


def test_empty_and_error_states():
    assert "v4 has not been fitted yet; it fits nightly at 3:15" in PAGE
    assert "isEmptyModel" in PAGE and "conf === 'none'" in PAGE
    assert "role', 'alert'" in PAGE and "Could not load prediction accuracy" in PAGE
    assert "id = 'retry'" in PAGE


def test_units_come_from_response():
    assert "S.data.unit" in PAGE
    assert "isC()" in PAGE
    assert "BIAS_LIMIT_F * 5 / 9" in PAGE  # the +/-0.5 F limit shown as degrees C on Celsius installs
    assert FIXTURE["unit"] == "°F"


def test_only_bundled_libraries():
    srcs = re.findall(r'<script[^>]+src="([^"]+)"', PAGE)
    assert sorted(srcs) == ["chart.umd.min.js", "chartjs-plugin-zoom.min.js", "hammer.min.js"]
    for s in srcs:
        assert (FRONTEND / s).is_file()
    assert "http://" not in PAGE and "https://" not in PAGE
    assert not re.search(r"<link[^>]+href=", PAGE)


def test_auth_matches_index_html():
    for frag in ("home-assistant", "hassTokens", "'Authorization'", "refreshAccessToken", "resp.status === 401"):
        assert frag in PAGE and frag in INDEX, frag


def test_responsive_and_theme():
    assert 'name="viewport"' in PAGE
    assert "prefers-color-scheme: dark" in PAGE
    assert "flex-wrap: wrap" in PAGE
    assert "overflow-x: hidden" in PAGE
    assert ":focus-visible" in PAGE


def _lum(h: str) -> float:
    h = h.lstrip("#")
    rgb = [int(h[i : i + 2], 16) / 255 for i in (0, 2, 4)]
    lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in rgb]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def _ratio(a: str, b: str) -> float:
    la, lb = sorted((_lum(a), _lum(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def _tokens(block: str) -> dict[str, str]:
    return dict(re.findall(r"--([a-z0-9]+):\s*(#[0-9a-fA-F]{6})", block))


def test_text_contrast_at_least_4_5():
    light = _tokens(re.search(r":root \{(.*?)\}", PAGE, re.DOTALL).group(1))
    dark = _tokens(re.search(r"prefers-color-scheme: dark\) \{\s*:root \{(.*?)\}", PAGE, re.DOTALL).group(1))
    for toks in (light, dark):
        for fg in ("ink", "ink2", "muted", "s1", "s2", "warn", "ok"):
            for bg in ("panel", "chip"):
                assert _ratio(toks[fg], toks[bg]) >= 4.5, (fg, bg, toks[fg], toks[bg])
        assert _ratio(toks["panel"], toks["ink"]) >= 4.5  # pressed button text on ink
        for fg in ("ink", "ink2", "muted"):
            assert _ratio(toks[fg], toks["bg"]) >= 4.5, fg


# ---- dashboard link ----------------------------------------------------------------------------


def test_index_links_to_page_in_analysis_tab():
    assert 'href="prediction_accuracy.html"' in INDEX
    assert "Prediction accuracy (preview)" in INDEX
    start = INDEX.index('id="tab-ai"')
    assert "prediction_accuracy.html" in INDEX[start : start + 3000]
    assert INDEX.count("prediction_accuracy") == 2  # the Analysis link and the chart's v4-gate note (R2)
