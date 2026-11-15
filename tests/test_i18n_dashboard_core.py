"""Issue #1341 (lane L9a): the dashboard core's words live in the catalog, and nothing drifts from it.

Occupant: a German or Arabic household opens the dashboard and every label, banner and sentence on the shell and the
Status tab is in their language; a developer who adds a label without a catalog entry (or edits the English in only one
place) is stopped here, not by a user seeing the wrong word.
"""

from __future__ import annotations

import html
import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
FE = ROOT / "custom_components" / "climate_advisor" / "frontend"
CAT = json.loads((FE / "i18n" / "dashboard" / "en.json").read_text(encoding="utf-8"))
PAGES = {
    "new_dashboard.html": (FE / "new_dashboard.html").read_text(encoding="utf-8"),
    "entry.html": (FE / "entry.html").read_text(encoding="utf-8"),
}
CORE_JS = {n: (FE / n).read_text(encoding="utf-8") for n in ("nd_core.js", "nd_status.js", "nd_night_card.js")}
MINE = re.compile(r"^dashboard\.(core|status|night|banner|entry)\.")


def _markup_pairs(src: str):
    """(key, english default) for every data-i18n element and data-i18n-attr attribute."""
    for m in re.finditer(r'<(\w+)\b([^>]*?\sdata-i18n="([^"]+)"[^>]*)>(.*?)</\1>', src, re.S):
        if "data-i18n-params" in m.group(2):
            yield m.group(3), None
        else:
            yield m.group(3), html.unescape(m.group(4))
    for m in re.finditer(r'<(\w+)\b([^>]*?\sdata-i18n-attr="([^"]+)"[^>]*)>', src, re.S):
        for pair in m.group(3).split(";"):
            attr, key = pair.split(":")
            val = re.search(r"\s" + re.escape(attr) + r'="([^"]*)"', m.group(2))
            assert val, f"{key}: attribute {attr} missing from the markup"
            yield key, html.unescape(val.group(1))


@pytest.mark.parametrize("page", sorted(PAGES))
def test_markup_default_text_is_the_catalog_english(page):
    """The markup keeps its English (no-script / failed-catalog default); it must equal the catalog's English."""
    seen = 0
    for key, default in _markup_pairs(PAGES[page]):
        seen += 1
        assert key in CAT, f"{page}: {key} is not in dashboard/en.json"
        if default is not None:
            assert default == CAT[key], f"{page}: {key} markup {default!r} != catalog {CAT[key]!r}"
    assert seen


def _literal_keys(src: str) -> set[str]:
    return set(re.findall(r"""['"](dashboard\.(?:core|status|night|banner|entry)\.[a-z0-9_]+)['"]""", src))


def test_every_key_the_js_names_is_in_the_catalog():
    for name, src in CORE_JS.items():
        missing = sorted(k for k in _literal_keys(src) if k not in CAT)
        assert not missing, f"{name} uses keys that dashboard/en.json lacks: {missing}"
    # keys the JS builds from a short prefix + a literal (S + 'x', N + 'x', B + 'x', t('x'))
    for name, prefix_var, area in (
        ("nd_status.js", "S", "status"),
        ("nd_night_card.js", "N", "night"),
        ("nd_core.js", "B", "banner"),
    ):
        src = CORE_JS[name]
        for key in re.findall(r"""\bt\('([a-z0-9_]+)'\s*[,)]""", src) + re.findall(
            rf"""\b{prefix_var} \+ '([a-z0-9_]+)'""", src
        ):
            if key.endswith("_"):  # a computed key: its prefix only
                continue
            assert f"dashboard.{area}.{key}" in CAT, f"{name}: dashboard.{area}.{key}"


# keys the JS picks by a computed suffix (a code word from the payload), so a literal scan cannot see their use
DYNAMIC = re.compile(
    r"^dashboard\.status\.(day_type_|occupancy_|treated_|role_|act_|fan_(off|running|unmanaged)$)|"
    r"^dashboard\.night\.act_|^dashboard\.core\.unit_|^dashboard\.banner\.(status_|treated_)"
)


def test_no_orphan_keys_in_the_areas_this_lane_owns():
    used = set()
    for src in list(CORE_JS.values()) + list(PAGES.values()):
        used |= _literal_keys(src)
    for src in PAGES.values():
        used |= {key for key, _ in _markup_pairs(src)}
    short = "\n".join(CORE_JS.values())
    for key in CAT:
        if not MINE.match(key) or key in used or DYNAMIC.match(key):
            continue
        name = key.split(".", 2)[2]
        assert re.search(rf"""['"]{re.escape(name)}['"]""", short), f"{key} is in the catalog but nothing uses it"


def _strip_calls(expr: str, names: tuple[str, ...]) -> str:
    """Remove every balanced call to one of ``names`` (their output is escaped by construction)."""
    out, i = "", 0
    pat = re.compile(r"(?<![\w.])(?:" + "|".join(re.escape(n) for n in names) + r")\(")
    while i < len(expr):
        m = pat.search(expr, i)
        if not m:
            out += expr[i:]
            break
        out += expr[i : m.start()]
        depth, j = 1, m.end()
        while j < len(expr) and depth:
            depth += {"(": 1, ")": -1}.get(expr[j], 0)
            j += 1
        i = j
    return out


def test_messages_reach_innerhtml_only_through_an_escaper():
    """Catalog text is shown with textContent / setAttribute, or escaped (esc / iso / th); never raw into innerHTML."""
    for name, src in CORE_JS.items():
        for m in re.finditer(r"innerHTML\s*[+]?=\s*([^;]*);", src):
            bare = _strip_calls(m.group(1), ("esc", "ND.esc", "iso", "ND.iso", "ND.th", "CAI18N.th", "I.esc"))
            assert not re.search(r"(?<![\w.])(?:ND\.t|CAI18N\.t|t|tw|ND\.text|CAI18N\.text)\(", bare), (
                f"{name}: innerHTML takes a message without an escaper: {m.group(1)[:140]}"
            )


def test_catalog_has_no_html_tags_and_parses_as_icu_with_named_params():
    for key, text in CAT.items():
        if not MINE.match(key):
            continue
        assert not re.search(r"<[a-z/]", text), f"{key}: HTML tags are not allowed in a message"
