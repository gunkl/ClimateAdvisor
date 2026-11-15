"""Python renderer == FormatJS/Intl for every row of the shared conformance fixture (Issue #1341).

The fixture is generated in Node by ``tools/i18n_conformance_gen.mjs``; ``tests/i18n_conformance.mjs`` renders the same
rows through the browser engine. No Node is needed here.

Row shapes (see the generator header): ``{lang, msg | key, params, catalogs?, strict?, expected}``. ``msg`` is an inline
pattern installed as the catalog key ``conformance.row``; ``key`` + ``catalogs`` render a key from tiny inline
catalogs; ``strict: false`` rows exercise the never-raise path (bad parameters, per-key and broken-translation
fallbacks); params may hold ``{"$f": "nan"|"inf"|"-inf"}`` float literals.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from custom_components.climate_advisor import i18n

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "i18n_conformance.json").read_text(encoding="utf-8"))
ROWS = FIXTURE["rows"]
KEY = "conformance.row"
_FLOATS = {"nan": float("nan"), "inf": float("inf"), "-inf": float("-inf")}


def _strip_isolates(text: str) -> str:
    return text.replace(i18n.FSI, "").replace(i18n.PDI, "")


def _revive(v):
    if isinstance(v, list):
        return [_revive(i) for i in v]
    if isinstance(v, dict):
        return _FLOATS[v["$f"]] if "$f" in v else {k: _revive(x) for k, x in v.items()}
    return v


def _render_row(row: dict) -> str:
    catalogs = {lang: dict(cat) for lang, cat in (row.get("catalogs") or {}).items()}
    if "msg" in row:
        catalogs.setdefault(row["lang"], {})[KEY] = row["msg"]
    i18n.use_catalogs(catalogs)
    params = {k: i18n.param_from_wire(_revive(v)) for k, v in row["params"].items()}
    return i18n.render(i18n.Msg(row.get("key", KEY), **params), row["lang"])


def test_fixture_header_and_coverage():
    header = FIXTURE["header"]
    assert header["intl_messageformat"] == "10.7.16"
    assert header["node"].startswith("v") and header["icu"] and header["cldr"]
    assert header["rows"] == len(ROWS) >= 1900
    assert {r["lang"] for r in ROWS} == set(i18n.LANGUAGES)
    for lang in i18n.LANGUAGES:
        assert sum(1 for r in ROWS if r["lang"] == lang and "plural" in r.get("msg", "")) >= 20
    # the review-fix groups are present (S2/S3/S4): durations, fallbacks, bad parameters, times, dates, NaN/inf
    kinds = {
        "dur": sum(1 for r in ROWS if "dur" in json.dumps(r["params"])),
        "fallback": sum(1 for r in ROWS if r.get("catalogs") and r.get("strict") is False),
        "bad": sum(1 for r in ROWS if r.get("strict") is False and "catalogs" not in r),
        "time": sum(1 for r in ROWS if '"$t": "time"' in json.dumps(r["params"])),
        "date": sum(1 for r in ROWS if '"$t": "date"' in json.dumps(r["params"])),
        "nan": sum(1 for r in ROWS if '"$f"' in json.dumps(r["params"])),
    }
    assert all(v >= 20 for v in kinds.values()), kinds


@pytest.mark.parametrize("lang", i18n.LANGUAGES)
def test_every_row_matches_formatjs(lang):
    rows = [r for r in ROWS if r["lang"] == lang]
    assert rows
    bad = []
    try:
        for r in rows:
            i18n.set_strict(r.get("strict", True))
            got = _strip_isolates(_render_row(r))
            if got != r["expected"]:
                bad.append((r.get("msg") or r.get("key"), r["params"], got, r["expected"]))
    finally:
        i18n.set_strict(True)
        i18n.reset_catalogs()
        i18n.load_catalogs()
    assert not bad, bad[:5]


def test_strict_rows_raise_in_strict_mode():
    """Every non-strict row is a programming error that strict mode (the test default) must surface."""
    i18n.set_strict(True)
    raising = 0
    try:
        for r in (r for r in ROWS if r.get("strict") is False):
            try:
                i18n.set_strict(True)
                _render_row(r)
            except i18n.I18nError:
                raising += 1
    finally:
        i18n.reset_catalogs()
        i18n.load_catalogs()
    nonstrict = [r for r in ROWS if r.get("strict") is False]
    # the per-key fallback rows of valid messages render cleanly in strict mode too; the bad-parameter and
    # broken-message rows are the ones that raise
    assert raising >= 80, (raising, len(nonstrict))


def test_arabic_wraps_every_argument_in_isolates():
    out = i18n.format_message("a {x} b {n, number} c {n, plural, other {#}}", "ar", {"x": "v", "n": 3})
    assert out == f"a {i18n.FSI}v{i18n.PDI} b {i18n.FSI}3{i18n.PDI} c {i18n.FSI}3{i18n.PDI}"


@pytest.mark.parametrize("lang", [lang for lang in i18n.LANGUAGES if lang != "ar"])
def test_only_arabic_wraps(lang):
    assert i18n.format_message("a {x} b {n, number}", lang, {"x": "v", "n": 3}) == "a v b 3"
