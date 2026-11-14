"""i18n engine: language resolution, ICU-subset parser/renderer, typed params, catalogs (Issue #1341)."""

from __future__ import annotations

import datetime as dt
import json
import logging
from pathlib import Path

import pytest

from custom_components.climate_advisor import i18n

FRONTEND = Path(__file__).resolve().parent.parent / "custom_components" / "climate_advisor" / "frontend"


@pytest.fixture(autouse=True)
def _real_catalogs():
    i18n.reset_catalogs()
    i18n.set_strict(True)
    i18n.load_catalogs()
    yield
    i18n.set_strict(None)
    i18n.reset_catalogs()


def _write_catalogs(root: Path, en: dict, **others: dict) -> Path:
    """A throwaway frontend dir whose ``common`` area holds the given catalogs (every other file is ``{}``)."""
    i18n_dir = root / "i18n"
    (i18n_dir).mkdir(parents=True)
    (i18n_dir / "index.json").write_text(json.dumps({"languages": list(i18n.LANGUAGES), "areas": list(i18n.AREAS)}))
    for area in i18n.AREAS:
        (i18n_dir / area).mkdir()
        for lang in i18n.LANGUAGES:
            data = en if (area, lang) == ("common", "en") else others.get(lang, {}) if area == "common" else {}
            (i18n_dir / area / f"{lang}.json").write_text(json.dumps(data))
    return root


# ------------------------------------------------------------------------------------------ resolve_language
@pytest.mark.parametrize(
    ("code", "expected"),
    [
        (None, "en"),
        ("", "en"),
        ("en", "en"),
        ("en-US", "en"),
        ("fr", "fr"),
        ("FR", "fr"),
        ("pt-BR", "pt"),
        ("pt_PT", "pt"),
        ("de-CH", "de"),
        ("zh", "zh-Hans"),
        ("zh-CN", "zh-Hans"),
        ("zh-SG", "zh-Hans"),
        ("zh-Hans", "zh-Hans"),
        ("zh-hans-cn", "zh-Hans"),
        ("zh-TW", "en"),
        ("zh-HK", "en"),
        ("zh-Hant", "en"),
        ("nb", "en"),
        ("xx-YY", "en"),
        ("ar-EG", "ar"),
        (42, "en"),
    ],
)
def test_resolve_language(code, expected):
    assert i18n.resolve_language(code) == expected


def test_hass_language():
    class Cfg:
        language = "pt-BR"

    class Hass:
        config = Cfg()

    assert i18n.hass_language(Hass()) == "pt"
    assert i18n.hass_language(object()) == "en"


# ------------------------------------------------------------------------------------------ catalogs
def test_index_json_is_the_single_source_of_the_lists():
    index = json.loads((FRONTEND / "i18n" / "index.json").read_text(encoding="utf-8"))
    assert tuple(index["languages"]) == i18n.LANGUAGES
    assert tuple(index["areas"]) == i18n.AREAS
    assert len(i18n.LANGUAGES) == 16
    for area in i18n.AREAS:
        for lang in i18n.LANGUAGES:
            assert (FRONTEND / "i18n" / area / f"{lang}.json").is_file()


def test_load_is_idempotent_and_flags():
    assert i18n.catalogs_loaded()
    first = i18n._CATALOGS
    i18n.load_catalogs()
    assert i18n._CATALOGS is first


def test_load_rejects_unprefixed_keys(tmp_path):
    root = _write_catalogs(tmp_path, {"wrong.key": "x"})
    with pytest.raises(i18n.I18nError):
        i18n.load_catalogs(root)


def test_load_rejects_index_mismatch(tmp_path):
    root = _write_catalogs(tmp_path, {})
    (root / "i18n" / "index.json").write_text(json.dumps({"languages": ["en"], "areas": ["common"]}))
    with pytest.raises(i18n.I18nError):
        i18n.load_catalogs(root)


def test_real_common_keys_render_in_every_language():
    for lang in i18n.LANGUAGES:
        for mode in ("heat", "cool", "off", "auto", "fan_only", "heat_cool"):
            text = i18n.render(i18n.Msg(f"common.mode.{mode}"), lang)
            assert text and text != f"common.mode.{mode}"
        assert i18n.render(i18n.Code("common.mode.cool"), lang) == i18n.render(i18n.Msg("common.mode.cool"), lang)
    assert i18n.render_en(i18n.Msg("common.mode.fan_only")) == "Fan only"
    assert i18n.render(i18n.Msg("common.unknown"), "fr") == "Inconnu"


# ------------------------------------------------------------------------------------------ missing keys / fallback
def test_missing_english_key_raises_in_strict_mode():
    with pytest.raises(i18n.MissingKeyError):
        i18n.render(i18n.Msg("common.nope"), "fr")


def test_missing_english_key_warns_once_and_returns_key_in_production(caplog):
    i18n.set_strict(False)
    i18n._WARNED.clear()
    with caplog.at_level(logging.WARNING):
        assert i18n.render(i18n.Msg("common.nope2"), "de") == "common.nope2"
        assert i18n.render(i18n.Msg("common.nope2"), "de") == "common.nope2"
    assert sum("common.nope2" in r.getMessage() for r in caplog.records) == 1


def test_per_key_fallback_to_english(tmp_path):
    root = _write_catalogs(tmp_path, {"common.a": "A {x}", "common.b": "B"}, fr={"common.a": "A fr {x}"})
    i18n.load_catalogs(root)
    assert i18n.render(i18n.Msg("common.a", x="1"), "fr") == "A fr 1"
    assert i18n.render(i18n.Msg("common.b"), "fr") == "B"  # missing in fr only
    assert i18n.render(i18n.Msg("common.a", x="1"), "pt-BR") == "A 1"  # pt has no file content


def test_broken_translation_falls_back_to_english_in_production(tmp_path):
    root = _write_catalogs(tmp_path, {"common.a": "A {x}"}, de={"common.a": "A {y}"})
    i18n.load_catalogs(root)
    with pytest.raises(i18n.I18nError):
        i18n.render(i18n.Msg("common.a", x="1"), "de")
    i18n.set_strict(False)
    i18n._WARNED.clear()
    assert i18n.render(i18n.Msg("common.a", x="1"), "de") == "A 1"


def test_str_passes_through_unchanged():
    assert i18n.render("plain {x} 'text'", "fr") == "plain {x} 'text'"
    assert i18n.render_en("already English") == "already English"


def test_render_resolves_language_codes(tmp_path):
    root = _write_catalogs(tmp_path, {"common.a": "en"}, pt={"common.a": "pt"}, **{"zh-Hans": {"common.a": "zh"}})
    i18n.load_catalogs(root)
    assert i18n.render(i18n.Msg("common.a"), "pt-BR") == "pt"
    assert i18n.render(i18n.Msg("common.a"), "zh-CN") == "zh"
    assert i18n.render(i18n.Msg("common.a"), "zh-TW") == "en"


# ------------------------------------------------------------------------------------------ parser
@pytest.mark.parametrize(
    "pattern",
    [
        "{",
        "{x",
        "{x,}",
        "{x, date}",
        "{x, time}",
        "{x, selectordinal, other {#}}",
        "{n, plural, one {a}}",
        "{n, plural, offset:1 other {#}}",
        "{n, plural, =1.5 {a} other {b}}",
        "{n, plural, one {a} one {b} other {c}}",
        "{x, select, a {A}}",
        "{n, number, percent}",
        "{n, number, ::integer}",
        "{n, number, ::currency/USD}",
        "<b>bold</b>",
        "a </b>",
        "{a, plural, other {{b, select, other {{c, select, other {x}}}}}}",
        "{x, plural other {a}}",
        "{n, plural, other {a}",
        "{}",
        "{1 x}",
    ],
)
def test_banned_or_malformed_syntax_is_rejected(pattern):
    with pytest.raises(i18n.MessageSyntaxError):
        i18n.parse_message(pattern)


def test_nesting_of_two_levels_is_allowed():
    out = i18n.format_message("{n, plural, other {{g, select, a {# A} other {# O}}}}", "en", {"n": 2, "g": "a"})
    assert out == "# A"  # '#' is literal inside the select, as in FormatJS


@pytest.mark.parametrize(
    ("pattern", "expected"),
    [
        ("It''s", "It's"),
        ("It's {x}", "It's v"),
        ("'{'x'}'", "{x}"),
        ("'{x}' and ''{x}''", "{x} and 'v'"),
        ("a '' b", "a ' b"),
        ("'{abc", "{abc"),
        ("'}' } x", "} } x"),
        ("rock 'n' roll", "rock 'n' roll"),
        ("'<' 5 < 6", "< 5 < 6"),
        ("# x", "# x"),
        ("''''", "''"),
        ("'{''}'", "{'}"),
        ("end'", "end'"),
    ],
)
def test_apostrophe_and_brace_rules(pattern, expected):
    assert i18n.format_message(pattern, "en", {"x": "v"}) == expected


def test_apostrophe_pound_only_quotes_in_plural():
    assert i18n.format_message("{n, plural, other {'#' # it's}}", "en", {"n": 5}) == "# 5 it's"
    assert i18n.format_message("'#' #", "en") == "'#' #"


def test_whitespace_inside_arguments():
    assert i18n.format_message("{ x }|{ n , plural , other { # } }", "en", {"x": "v", "n": 2}) == "v| 2 "


def test_plural_exact_match_beats_category_and_decimals_use_other():
    pattern = "{n, plural, =0 {none} =1 {exactly one} one {# item} other {# items}}"
    assert i18n.format_message(pattern, "en", {"n": 0}) == "none"
    assert i18n.format_message(pattern, "en", {"n": 1}) == "exactly one"
    assert i18n.format_message(pattern, "en", {"n": 1.0}) == "exactly one"
    assert i18n.format_message(pattern, "en", {"n": 1.5}) == "1.5 items"
    assert i18n.format_message(pattern, "en", {"n": 1234}) == "1,234 items"
    assert i18n.format_message(pattern, "fr", {"n": 1234}) == "1 234 items"


def test_plural_falls_back_to_other_when_category_absent():
    assert i18n.format_message("{n, plural, one {a} other {b}}", "ru", {"n": 3}) == "b"


def test_select_uses_other_for_unknown_values():
    pattern = "{g, select, f {F} m {M} other {O}}"
    assert [i18n.format_message(pattern, "en", {"g": g}) for g in ("f", "m", "q")] == ["F", "M", "O"]


def test_plain_numeric_argument_is_javascript_string_not_locale_formatted():
    assert i18n.format_message("{n}", "fr", {"n": 1234.5}) == "1234.5"
    assert i18n.format_message("{n}", "en", {"n": 3.0}) == "3"
    assert i18n.format_message("{n}", "en", {"n": 0.00001}) == "0.00001"


def test_errors_for_bad_values():
    with pytest.raises(i18n.I18nError):
        i18n.format_message("{x}", "en", {})
    with pytest.raises(i18n.I18nError):
        i18n.format_message("{n, plural, other {#}}", "en", {"n": "3"})
    with pytest.raises(i18n.I18nError):
        i18n.format_message("{n, number}", "en", {"n": "3"})
    with pytest.raises(i18n.I18nError):
        i18n.format_message("{g, select, other {x}}", "en", {"g": None})


def test_parse_cache_returns_same_ast():
    assert i18n.parse_message("hello {x}") is i18n.parse_message("hello {x}")


# ------------------------------------------------------------------------------------------ typed params + wire
def _sample_msg() -> i18n.Msg:
    inner = i18n.Msg("common.mode.cool")
    return i18n.Msg(
        "m.k",
        name="Salon",
        count=3,
        ratio=0.5,
        t=i18n.Temp(72.3, "fahrenheit", 1),
        d=i18n.TempDelta(3.0, "celsius", 0),
        at=i18n.Time(dt.datetime(2026, 11, 14, 15, 5, tzinfo=dt.timezone(dt.timedelta(hours=-5)))),
        on=i18n.Date(dt.datetime(2026, 11, 14, 15, 5)),
        span=i18n.Duration(300),
        both=i18n.ListOf(["a", "b"], style="or"),
        mode=i18n.Code("common.mode.heat"),
        nested=inner,
    )


def test_wire_round_trip_and_json_safe():
    msg = _sample_msg()
    wire = msg.to_wire()
    assert json.loads(json.dumps(wire)) == wire
    assert wire["params"]["t"] == {"$t": "temp", "f": 72.3, "unit": "fahrenheit", "d": 1}
    assert wire["params"]["d"] == {"$t": "tdelta", "f": 3.0, "unit": "celsius", "d": 0}
    assert wire["params"]["span"] == {"$t": "dur", "s": 300}
    assert wire["params"]["mode"] == {"$t": "code", "key": "common.mode.heat"}
    assert wire["params"]["both"] == {"$t": "list", "items": ["a", "b"], "style": "or"}
    assert wire["params"]["nested"] == {"$t": "msg", "key": "common.mode.cool", "params": {}}
    assert wire["params"]["at"]["ts"] == "2026-11-14T15:05:00-05:00"
    assert i18n.Msg.from_wire(json.loads(json.dumps(wire))) == msg


def test_typed_params_render_through_a_message(tmp_path):
    root = _write_catalogs(
        tmp_path,
        {
            "common.mode.cool": "Cool",
            "common.mode.heat": "Heat",
            "common.k": "{t}|{d}|{at}|{on}|{span}|{both}|{mode}|{nested}|{count}",
            "common.duration.hours_frac": "{n, number, ::.0}h",
        },
        fr={
            "common.mode.cool": "Froid",
            "common.k": "{t}|{d}|{at}|{on}|{span}|{both}|{mode}|{nested}|{count}",
            "common.duration.hours_frac": "{n, number, ::.0}\u00a0h",
        },
    )
    i18n.load_catalogs(root)
    msg = i18n.Msg(
        "common.k",
        t=i18n.Temp(72.3, "fahrenheit", 1),
        d=i18n.TempDelta(9.0, "celsius", 0),
        at=i18n.Time(dt.datetime(2026, 11, 14, 15, 5)),
        on=i18n.Date(dt.datetime(2026, 11, 14)),
        span=i18n.Duration(5400),
        both=i18n.ListOf(["a", "b", "c"], style="or"),
        mode=i18n.Code("common.mode.heat"),
        nested=i18n.Msg("common.mode.cool"),
        count=3,
    )
    assert i18n.render_en(msg) == "72.3°F|5°C|3:05 PM|Nov 14|1.5h|a, b, or c|Heat|Cool|3"
    assert i18n.render(msg, "fr") == "72,3°F|5°C|15:05|14 nov.|1,5 h|a, b ou c|Heat|Froid|3"


def test_nested_msg_in_arabic_is_isolated_once_per_level(tmp_path):
    root = _write_catalogs(
        tmp_path, {"common.i": "in {x}", "common.o": "out {m}"}, ar={"common.i": "في {x}", "common.o": "خارج {m}"}
    )
    i18n.load_catalogs(root)
    out = i18n.render(i18n.Msg("common.o", m=i18n.Msg("common.i", x="v")), "ar")
    assert out == f"خارج {i18n.FSI}في {i18n.FSI}v{i18n.PDI}{i18n.PDI}"


def test_unknown_wire_type_and_bad_values_raise():
    with pytest.raises(i18n.I18nError):
        i18n.param_from_wire({"$t": "nope"})
    with pytest.raises(i18n.I18nError):
        i18n.param_to_wire(object())
    with pytest.raises(i18n.I18nError):
        i18n.param_from_wire(True)


def test_listof_items_may_be_numbers_and_typed():
    out = i18n.format_message("{l}", "de", {"l": i18n.ListOf([1234.5, i18n.Temp(70.0, "fahrenheit"), "x"])})
    assert out == "1.234,5, 70°F und x"


def test_engine_has_no_homeassistant_import():
    source = (Path(i18n.__file__)).read_text(encoding="utf-8")
    assert "homeassistant" not in source.replace("``homeassistant``", "")
