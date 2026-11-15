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
RESOLUTION = json.loads(
    (Path(__file__).parent / "fixtures" / "i18n_language_resolution.json").read_text(encoding="utf-8")
)


@pytest.mark.parametrize(("code", "expected"), RESOLUTION["cases"], ids=[repr(c[0]) for c in RESOLUTION["cases"]])
def test_resolve_language(code, expected):
    """The table is shared with tests/i18n_runtime.mjs (CAI18N.resolveLanguage): one table drives both runtimes."""
    assert i18n.resolve_language(code) == expected


def test_resolution_fixture_lists_the_shipped_languages():
    assert tuple(RESOLUTION["languages"]) == i18n.LANGUAGES
    assert any(code == "zh-Hans-TW" for code, _ in RESOLUTION["cases"])  # an explicit Hans script wins over a region


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
    assert i18n.format_message("{n}", "en", {"n": 0.000001}) == "0.000001"
    assert i18n.format_message("{n}", "en", {"n": 1e-7}) == "1e-7"  # JavaScript's exponent form below 1e-6
    assert i18n.format_message("{n}", "en", {"n": 1.5e-9}) == "1.5e-9"
    assert i18n.format_message("{n}", "en", {"n": 1e22}) == "1e+22"


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
        on=i18n.Date(dt.datetime(2026, 11, 14, 15, 5, tzinfo=dt.timezone(dt.timedelta(hours=-5)))),
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
        at=i18n.Time(dt.datetime(2026, 11, 14, 15, 5, tzinfo=dt.UTC)),
        on=i18n.Date(dt.datetime(2026, 11, 14, tzinfo=dt.UTC)),
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


# ------------------------------------------------------------------------------------------ review fixes (Issue #1341)
def _by_sentence(sentence: str, **catalogs: dict) -> None:
    """Install tiny in-memory catalogs: English ``t.m`` plus per-language overrides."""
    i18n.use_catalogs({"en": {"t.m": sentence}, **catalogs})


def test_inline_catalogs_report_their_source():
    assert i18n.catalogs_source() == i18n.default_i18n_dir()
    i18n.use_catalogs({"en": {"t.m": "x"}})
    assert i18n.catalogs_source() == i18n.INLINE_SOURCE and i18n.render(i18n.Msg("t.m"), "en") == "x"


def test_js_language_and_area_lists_match_index_json():
    """S8: the offline-fallback lists inside frontend/i18n.js must equal index.json (the one source)."""
    import re

    js = (FRONTEND / "i18n.js").read_text(encoding="utf-8")

    def _list(name: str) -> list[str]:
        m = re.search(rf"var {name} = \[(.*?)\];", js, re.S)
        assert m, name
        return re.findall(r"'([^']+)'", m.group(1))

    index = json.loads((FRONTEND / "i18n" / "index.json").read_text(encoding="utf-8"))
    assert _list("LANGUAGES") == index["languages"]
    assert _list("AREAS") == index["areas"]


@pytest.mark.parametrize(
    "bad",
    [
        i18n.Duration(None),
        i18n.Duration(float("nan")),
        i18n.Duration(float("inf")),
        i18n.Duration("300"),
        i18n.Temp(None, "celsius"),
        i18n.Temp("72", "fahrenheit"),
        i18n.TempDelta(None, "celsius"),
        i18n.Time("12:00"),
        i18n.Time(None),
        i18n.Date("2026-11-14"),
        i18n.Date(dt.date(2026, 11, 14)),
        None,
        True,
        {"a": 1},
        [1, 2],
        i18n.ListOf([1, None, object]),
        i18n.Code(None),
    ],
    ids=repr,
)
def test_bad_parameter_shows_an_em_dash_and_never_raises_outside_strict_mode(bad, caplog):
    """S2. Occupant: a notification with one unusable value still reads as a sentence ("Fan off for \u2014")
    instead of vanishing or crashing the briefing."""
    _by_sentence("a={x}")
    i18n.set_strict(False)
    caplog.set_level(logging.WARNING, logger="custom_components.climate_advisor")
    out = i18n.render(i18n.Msg("t.m", x=bad), "de")
    if isinstance(bad, i18n.ListOf):
        assert i18n.BAD_PARAM in out
    elif isinstance(bad, i18n.Code):
        assert out == "a="  # a code with no key renders as nothing, like a message with no key
    else:
        assert out == f"a={i18n.BAD_PARAM}"
    i18n.set_strict(True)
    with pytest.raises(i18n.I18nError):
        i18n.render(i18n.Msg("t.m", x=bad), "de")


def test_bad_parameter_is_logged_once_per_key_and_type(caplog):
    _by_sentence("a={x}")
    i18n.set_strict(False)
    i18n._WARNED.clear()
    caplog.set_level(logging.WARNING, logger="custom_components.climate_advisor")
    for _ in range(5):
        i18n.render(i18n.Msg("t.m", x=i18n.Duration(None)), "en")
    assert len([r for r in caplog.records if "Duration parameter" in r.getMessage()]) == 1


def test_a_failing_message_falls_back_to_english_then_the_key(caplog):
    _by_sentence("en {n}", de={"t.m": "de {n, plural, one {# x}"})
    i18n.set_strict(False)
    assert i18n.render(i18n.Msg("t.m", n=3), "de") == "en 3"  # the English message, in English
    _by_sentence("{n, plural, one {#}", de={"t.m": "{n, plural, one {#}"})
    assert i18n.render(i18n.Msg("t.m", n=3), "de") == "t.m"
    assert i18n.render(i18n.Msg("t.m", n=3), "en") == "t.m"
    i18n.set_strict(True)
    with pytest.raises(i18n.MessageSyntaxError):
        i18n.render(i18n.Msg("t.m", n=3), "de")


def test_render_never_raises_on_an_unexpected_exception(monkeypatch):
    _by_sentence("a {n}")
    monkeypatch.setattr(i18n, "parse_message", lambda p: 1 / 0)
    i18n.set_strict(False)
    assert i18n.render(i18n.Msg("t.m", n=1), "de") == "t.m"
    i18n.set_strict(True)
    with pytest.raises(ZeroDivisionError):
        i18n.render(i18n.Msg("t.m", n=1), "de")


def test_render_of_something_that_is_not_a_message():
    i18n.set_strict(False)
    assert i18n.render(None, "en") == "" and i18n.render(5, "en") == ""
    i18n.set_strict(True)
    with pytest.raises(i18n.I18nError):
        i18n.render(None, "en")


def test_a_message_with_no_key_renders_as_nothing():
    i18n.set_strict(False)
    assert i18n.render(i18n.Msg(""), "en") == ""
    assert i18n.render(i18n.Msg.from_wire({}), "en") == ""
    i18n.set_strict(True)
    with pytest.raises(i18n.MissingKeyError):
        i18n.render(i18n.Msg(""), "en")
    with pytest.raises(i18n.I18nError):
        i18n.Msg.from_wire({})


@pytest.mark.parametrize(
    "wire",
    [
        {"$t": "temp"},
        {"$t": "temp", "f": 70},
        {"$t": "time"},
        {"$t": "time", "ts": "garbage"},
        {"$t": "time", "ts": "2026-13-45T25:61:00+00:00"},
        {"$t": "date", "ts": 5},
        {"$t": "dur"},
        {"$t": "list"},
        {"$t": "list", "items": "ab"},
        {"$t": "code"},
        {"$t": "msg"},
        {"$t": "nope"},
        {"$t": "bad"},
        None,
        [1],
        {"x": 1},
    ],
    ids=repr,
)
def test_malformed_wire_param_renders_as_an_em_dash_or_nothing(wire):
    """S2: ``Msg.from_wire`` never raises outside strict mode; the bad parameter shows as an em dash."""
    _by_sentence("a={x}")
    i18n.set_strict(False)
    msg = i18n.Msg.from_wire({"key": "t.m", "params": {"x": wire}})
    out = i18n.render(msg, "de")
    assert out in (f"a={i18n.BAD_PARAM}", "a=")  # a msg param with no key renders as nothing
    i18n.set_strict(True)
    with pytest.raises(i18n.I18nError):
        i18n.Msg.from_wire({"key": "t.m", "params": {"x": wire}})


def test_malformed_wire_params_container():
    i18n.set_strict(False)
    assert i18n.Msg.from_wire({"key": "common.unknown", "params": "junk"}).params == {}
    assert i18n.render(i18n.Msg.from_wire({"key": "common.unknown", "params": [1]}), "en") == "Unknown"


def test_param_to_wire_never_emits_json_breaking_values():
    """NaN/inf cannot travel as JSON (the whole /status response would not parse): they leave as a bad marker."""
    i18n.set_strict(False)
    wire = i18n.Msg("t.m", a=float("nan"), b=i18n.Temp(float("inf"), "celsius"), c=None, d=i18n.Time("x")).to_wire()
    assert {v["$t"] for v in wire["params"].values()} == {"bad"}
    json.dumps(wire, allow_nan=False)
    i18n.set_strict(True)
    with pytest.raises(i18n.I18nError):
        i18n.param_to_wire(float("nan"))


def test_nan_and_infinity_follow_intl():
    """S2: plural category "other", text as Intl prints it ("NaN" / infinity; ar and ru have their own NaN words)."""
    for value in (float("nan"), float("inf"), float("-inf")):
        for lang in i18n.LANGUAGES:
            assert i18n.plural_category(value, lang) == "other"
    assert i18n.format_message("{n, number}", "en", {"n": float("nan")}) == "NaN"
    assert i18n.format_message("{n, number}", "en", {"n": float("inf")}) == "\u221e"
    assert i18n.format_message("{n, number, ::percent}", "de", {"n": float("-inf")}) == "-\u221e\u00a0%"
    assert (
        i18n.format_message("{n, number}", "ru", {"n": float("nan")})
        == "\u043d\u0435\u00a0\u0447\u0438\u0441\u043b\u043e"
    )
    assert i18n.format_message("{n, plural, one {o} other {x #}}", "en", {"n": float("nan")}) == "x NaN"
    assert i18n.format_message("{n}", "en", {"n": float("nan")}) == "NaN"
    assert i18n.format_message("{n}", "en", {"n": float("inf")}) == "Infinity"
    assert (
        i18n.format_message("{n, number}", "en", {"n": 1e300}).count(",") == 100
    )  # a huge finite number is not an error


def test_temperature_nan_and_inf_print_like_intl_in_english_too():
    _by_sentence("{x}")
    assert i18n.render(i18n.Msg("t.m", x=i18n.Temp(float("nan"), "celsius", 1)), "en") == "NaN\u00b0C"
    assert i18n.render(i18n.Msg("t.m", x=i18n.TempDelta(float("-inf"), "fahrenheit")), "de") == "-\u221e\u00b0F"


def test_english_duration_ties_use_python_half_even():
    """S3: 75 and 135 minutes are exact ties (1.25 h, 2.25 h): Python's f"{x:.1f}" gives 1.2 and 2.2."""
    assert [i18n.format_duration(m * 60, "en") for m in (75, 135, 105, 165)] == ["1.2h", "2.2h", "1.8h", "2.8h"]
    assert i18n.format_duration(150, "en") == "2 min"  # 2.5 minutes -> half-even 2


def test_duration_rejects_unusable_seconds():
    for bad in (None, float("nan"), float("inf"), "300", True):
        with pytest.raises(i18n.I18nError):
            i18n.format_duration(bad, "en")


def test_time_and_date_take_aware_datetimes_and_show_the_zone_they_are_given(caplog):
    """S4: callers pass HA-local datetimes; the wall clock of the datetime as given is shown, ts keeps the offset."""
    est = dt.timezone(dt.timedelta(hours=-5))
    at = dt.datetime(2026, 11, 14, 15, 5, tzinfo=est)
    _by_sentence("{t} | {d}")
    assert i18n.render(i18n.Msg("t.m", t=i18n.Time(at), d=i18n.Date(at)), "en") == "3:05 PM | Nov 14"
    assert i18n.Time(at).to_wire() == {"$t": "time", "ts": "2026-11-14T15:05:00-05:00"}
    # the same instant in UTC shows the UTC wall clock: the datetime's own zone decides
    assert i18n.render(
        i18n.Msg("t.m", t=i18n.Time(at.astimezone(dt.UTC)), d=i18n.Date(at.astimezone(dt.UTC))), "en"
    ) == ("8:05 PM | Nov 14")
    # a naive datetime is taken as UTC and logged once
    i18n._WARNED.clear()
    caplog.set_level(logging.WARNING, logger="custom_components.climate_advisor")
    naive = dt.datetime(2026, 11, 14, 15, 5)
    assert i18n.Time(naive).to_wire()["ts"] == "2026-11-14T15:05:00+00:00"
    assert i18n.render(i18n.Msg("t.m", t=i18n.Time(naive), d=i18n.Date(naive)), "en") == "3:05 PM | Nov 14"
    assert len([r for r in caplog.records if "naive datetime" in r.getMessage()]) == 1
    # round trip keeps the instant and the offset
    back = i18n.param_from_wire(i18n.Time(at).to_wire())
    assert back.dt == at and back.dt.utcoffset() == at.utcoffset()


@pytest.mark.parametrize(
    ("pattern", "params", "expected"),
    [
        ("{ n }", {"n": 1}, "1"),
        ("{\v n \f}", {"n": 1}, "1"),
        ("{\u200e n}", {"n": 1}, "1"),
        ("{\u2028n\u2029}", {"n": 1}, "1"),
        ("{\x85n\x85}", {"n": 1}, "1"),
        ("{n,\u200e plural,\u200f other {x #}\u200f}", {"n": 2}, "x 2"),
        ("{n,\vnumber,\f::percent}", {"n": 0.5}, "50%"),
        ("{n, select, a {A}\v\f other {O}}", {"n": "z"}, "O"),
        ("a <\u00e9 b", {}, "a <\u00e9 b"),
        ("<\u65e5\u672c>", {}, "<\u65e5\u672c>"),
        ("a <1 b < c <_x>", {}, "a <1 b < c <_x>"),
        ("{n, number, ::.0}", {"n": 1.25}, "1.3"),
        ("{n, number, ::.00}", {"n": 1.255}, "1.26"),
    ],
)
def test_parser_whitespace_and_angle_brackets_follow_formatjs(pattern, params, expected):
    """N2 (each expectation verified against FormatJS 10.7.16): Pattern_White_Space between argument tokens, and a
    ``<`` that is not followed by an ASCII letter or ``/`` is literal text."""
    assert i18n.format_message(pattern, "en", params) == expected


@pytest.mark.parametrize(
    "pattern",
    [
        "<b>x</b>",
        "<B>",
        "a </ b",
        "a </\u00e9",
        "{n\u200e}",  # FormatJS keeps a trailing LRM in the identifier: an unknown argument
        "{n, number\u200e}",
        "{n, plural, other\u200e {x}}",  # the LRM stays in the selector name: no "other" clause, as in FormatJS
        "{n, number, ::.000}",
        "{n, number, ::.0#}",
        "{n, number, ::integer}",
        "{n, number, ::currency/USD}",
        "{n,\u00a0plural, other {x}}",  # NBSP is not Pattern_White_Space
    ],
)
def test_parser_rejections_that_formatjs_also_makes(pattern):
    with pytest.raises(i18n.I18nError):
        i18n.format_message(pattern, "en", {"n": 1})


def test_warn_once_dedups_on_the_message_not_the_varying_error_text(caplog):
    """Issue #1341 N5: a render failing with a different value each time must not grow ``_WARNED`` without bound."""
    import logging

    i18n._WARNED.clear()
    with caplog.at_level(logging.WARNING):
        for n in range(5):
            i18n._warn_once("cannot render 'k' in de (ValueError)", f"bad value {n}")
    assert len(i18n._WARNED) == 1
    assert len([r for r in caplog.records if "cannot render" in r.getMessage()]) == 1


def test_msg_of_and_code_of_understand_every_carrier():
    from custom_components.climate_advisor.record_text import MsgText
    from custom_components.climate_advisor.status_text import CodedText
    from custom_components.climate_advisor.timeline_surfaces import CardText

    m = i18n.Msg("common.mode.cool")
    for carrier in (m, MsgText(m), CodedText("c", m), CardText("x", m)):
        assert i18n.msg_of(carrier) is m
    assert i18n.code_of(CodedText("c", m)) == "c"
    for plain in ("text", None, 3, CodedText("c", None, text="t"), CardText("x")):
        assert i18n.msg_of(plain) is None
    assert i18n.code_of("text") is None and i18n.code_of(m, "d") == "d"
