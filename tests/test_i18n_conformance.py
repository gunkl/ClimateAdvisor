"""Python renderer == FormatJS/Intl for every row of the shared conformance fixture (Issue #1341).

The fixture is generated in Node by ``tools/i18n_conformance_gen.mjs``; ``tests/i18n_conformance.mjs`` renders the same
rows through the browser engine. No Node is needed here.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from custom_components.climate_advisor import i18n

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "i18n_conformance.json").read_text(encoding="utf-8"))
ROWS = FIXTURE["rows"]


def _strip_isolates(text: str) -> str:
    return text.replace(i18n.FSI, "").replace(i18n.PDI, "")


def _render_row(row: dict) -> str:
    params = {k: i18n.param_from_wire(v) for k, v in row["params"].items()}
    return i18n.format_message(row["msg"], row["lang"], params)


def test_fixture_header_and_coverage():
    header = FIXTURE["header"]
    assert header["intl_messageformat"] == "10.7.16"
    assert header["node"].startswith("v") and header["icu"] and header["cldr"]
    assert header["rows"] == len(ROWS) >= 1000
    assert {r["lang"] for r in ROWS} == set(i18n.LANGUAGES)
    for lang in i18n.LANGUAGES:
        assert sum(1 for r in ROWS if r["lang"] == lang and "plural" in r["msg"]) >= 20


@pytest.mark.parametrize("lang", i18n.LANGUAGES)
def test_every_row_matches_formatjs(lang):
    rows = [r for r in ROWS if r["lang"] == lang]
    assert rows
    bad = [
        (r["msg"], r["params"], got, r["expected"])
        for r in rows
        if (got := _strip_isolates(_render_row(r))) != r["expected"]
    ]
    assert not bad, bad[:5]


def test_arabic_wraps_every_argument_in_isolates():
    out = i18n.format_message("a {x} b {n, number} c {n, plural, other {#}}", "ar", {"x": "v", "n": 3})
    assert out == f"a {i18n.FSI}v{i18n.PDI} b {i18n.FSI}3{i18n.PDI} c {i18n.FSI}3{i18n.PDI}"


@pytest.mark.parametrize("lang", [lang for lang in i18n.LANGUAGES if lang != "ar"])
def test_only_arabic_wraps(lang):
    assert i18n.format_message("a {x} b {n, number}", lang, {"x": "v", "n": 3}) == "a v b 3"
