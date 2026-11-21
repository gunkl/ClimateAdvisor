"""Formatters: English byte-identical contract and per-language samples (Issue #1341)."""

from __future__ import annotations

import datetime as dt
import math
from pathlib import Path

import pytest

from custom_components.climate_advisor import i18n
from custom_components.climate_advisor.temperature import (
    CELSIUS,
    FAHRENHEIT,
    UNIT_SYMBOL,
    convert_delta,
    format_temp,
    format_temp_delta,
    from_fahrenheit,
)


@pytest.fixture(autouse=True)
def _catalogs():
    i18n.reset_catalogs()
    i18n.set_strict(True)
    i18n.load_catalogs()
    yield
    i18n.set_strict(None)
    i18n.reset_catalogs()


# ------------------------------------------------------------------------------------------ English byte-identical
def test_format_time_en_equals_the_legacy_strftime_for_every_minute():
    for minute_of_day in range(24 * 60):
        moment = dt.datetime(2026, 11, 14, minute_of_day // 60, minute_of_day % 60)
        legacy = moment.strftime("%I:%M %p").lstrip("0")
        assert i18n.format_time(moment, "en") == legacy
        assert i18n.format_time(moment, "en-US") == legacy
    assert i18n.format_time(dt.datetime(2026, 1, 1, 15, 5), "en") == "3:05 PM"
    assert i18n.format_time(dt.datetime(2026, 1, 1, 0, 5), "en") == "12:05 AM"
    assert i18n.format_time(dt.datetime(2026, 1, 1, 12, 0), "en") == "12:00 PM"


def test_format_time_en_equals_the_briefing_format_string():
    moment = dt.datetime(2026, 11, 14, 9, 30)
    assert i18n.format_time(moment, "en") == moment.strftime("%-I:%M %p")


def _legacy_temp(value_f, unit, decimals):
    return f"{from_fahrenheit(value_f, unit):.{decimals}f}{UNIT_SYMBOL.get(unit, chr(176) + 'F')}"


def _legacy_delta(delta_f, unit, decimals):
    return f"{convert_delta(delta_f, unit):.{decimals}f}{UNIT_SYMBOL.get(unit, chr(176) + 'F')}"


@pytest.mark.parametrize("unit", [FAHRENHEIT, CELSIUS, "kelvin"])
@pytest.mark.parametrize("decimals", [0, 1, 2])
def test_format_temp_english_is_byte_identical_for_none_and_en(unit, decimals):
    values = [-40.0, -4.1, -0.3, 0.0, 0.5, 2.5, 32.0, 68.0, 72.3, 72.5, 85.0, 99.99, 1234.567]
    for value in values:
        want = _legacy_temp(value, unit, decimals)
        assert format_temp(value, unit, decimals) == want
        assert format_temp(value, unit, decimals, lang=None) == want
        assert format_temp(value, unit, decimals, lang="en") == want
        assert format_temp(value, unit, decimals, lang="en-GB") == want
        want_delta = _legacy_delta(value, unit, decimals)
        assert format_temp_delta(value, unit, decimals) == want_delta
        assert format_temp_delta(value, unit, decimals, lang=None) == want_delta
        assert format_temp_delta(value, unit, decimals, lang="en") == want_delta


def test_format_temp_documented_examples_unchanged():
    assert format_temp(72.0, FAHRENHEIT) == "72°F"
    assert format_temp(72.0, CELSIUS) == "22°C"
    assert format_temp(72.5, CELSIUS, 1) == "22.5°C"
    assert format_temp_delta(9.0, CELSIUS) == "5°C"


def test_format_temp_other_languages_use_locale_separators():
    assert format_temp(72.5, CELSIUS, 1, lang="fr") == "22,5°C"
    assert format_temp(72.5, CELSIUS, 1, lang="de") == "22,5°C"
    assert format_temp(72.5, CELSIUS, 1, lang="ja") == "22.5°C"
    assert format_temp(72.3, FAHRENHEIT, 0, lang="ar") == "72°F"
    assert format_temp(-4.0, CELSIUS, 1, lang="ar") == "‎-20.0°C"
    assert format_temp(1234.56, FAHRENHEIT, 1, lang="fr") == "1234,6°F"  # no grouping in temperatures
    assert format_temp_delta(9.0, CELSIUS, 1, lang="tr") == "5,0°C"
    assert format_temp(72.3, FAHRENHEIT, 0, lang="pt-BR") == "72°F"


# ------------------------------------------------------------------------------------------ times and dates
SAMPLE = dt.datetime(2026, 11, 14, 15, 5)
MORNING = dt.datetime(2026, 11, 14, 9, 5)
MIDNIGHT = dt.datetime(2026, 11, 14, 0, 5)

TIME_SAMPLES = {
    "en": ("3:05 PM", "9:05 AM", "12:05 AM"),
    "fr": ("15:05", "9:05", "0:05"),
    "es": ("15:05", "9:05", "0:05"),
    "de": ("15:05", "9:05", "0:05"),
    "nl": ("15:05", "9:05", "0:05"),
    "ja": ("15:05", "9:05", "0:05"),
    "pt": ("15:05", "9:05", "0:05"),
    "it": ("15:05", "9:05", "0:05"),
    "pl": ("15:05", "9:05", "0:05"),
    "uk": ("15:05", "9:05", "0:05"),
    "ko": ("오후 3:05", "오전 9:05", "오전 12:05"),
    "hi": ("3:05 pm", "9:05 am", "12:05 am"),
    "ar": ("3:05 م", "9:05 ص", "12:05 ص"),
    "ru": ("15:05", "9:05", "0:05"),
    "zh-Hans": ("15:05", "9:05", "0:05"),
    "zh-Hant": ("下午3:05", "上午9:05", "上午12:05"),
    "tr": ("15:05", "9:05", "0:05"),
}
DATE_SAMPLES = {
    "en": "Nov 14",
    "fr": "14 nov.",
    "es": "14 nov",
    "de": "14. Nov.",
    "nl": "14 nov",
    "ja": "11月14日",
    "pt": "14 de nov.",
    "it": "14 nov",
    "pl": "14 lis",
    "uk": "14 лист.",
    "ko": "11월 14일",
    "hi": "14 नव॰",
    "ar": "14 نوفمبر",
    "ru": "14 нояб.",
    "zh-Hans": "11月14日",
    "zh-Hant": "11月14日",
    "tr": "14 Kas",
}


@pytest.mark.parametrize("lang", i18n.LANGUAGES)
def test_time_and_date_sample_per_language(lang):
    assert (
        i18n.format_time(SAMPLE, lang),
        i18n.format_time(MORNING, lang),
        i18n.format_time(MIDNIGHT, lang),
    ) == TIME_SAMPLES[lang]
    assert i18n.format_date(SAMPLE, lang) == DATE_SAMPLES[lang]


def test_times_and_dates_contain_no_nonbreaking_spaces():
    for lang in i18n.LANGUAGES:
        for moment in (SAMPLE, MORNING, MIDNIGHT):
            text = i18n.format_time(moment, lang) + i18n.format_date(moment, lang)
            assert " " not in text and " " not in text


def test_every_month_has_a_date_template_with_a_day():
    for lang in i18n.LANGUAGES:
        for month in range(1, 13):
            text = i18n.format_date(dt.date(2026, month, 7), lang)
            assert "7" in text and "{" not in text


def test_time_accepts_time_and_aware_datetime():
    assert i18n.format_time(dt.time(21, 7), "en") == "9:07 PM"
    aware = dt.datetime(2026, 1, 1, 21, 7, tzinfo=dt.timezone(dt.timedelta(hours=-8)))
    assert i18n.format_time(aware, "fr") == "21:07"


# ------------------------------------------------------------------------------------------ durations
def test_duration_english_matches_the_existing_grace_timer_text():
    def legacy(duration_s):
        minutes = max(1, round(duration_s / 60))
        if minutes >= 60 and minutes % 60 == 0:
            return f"{minutes // 60}h"
        if minutes >= 60:
            return f"{minutes / 60:.1f}h"
        return f"{minutes} min"

    for seconds in [0, 20, 30, 60, 90, 300, 1800, 3540, 3600, 3780, 4000, 5400, 7200, 9000, 86400, 100000]:
        assert i18n.format_duration(seconds, "en") == legacy(seconds), seconds
    assert i18n.format_duration(300, "en") == "5 min"
    assert i18n.format_duration(7200, "en") == "2h"
    assert i18n.format_duration(5400, "en") == "1.5h"


@pytest.mark.parametrize("lang", i18n.LANGUAGES)
def test_duration_is_a_translated_message_in_every_language(lang):
    texts = {i18n.format_duration(s, lang) for s in (300, 7200, 5400)}
    assert len(texts) == 3 and all(t for t in texts)
    assert i18n.format_duration(300, lang) == i18n.render(i18n.Msg("common.duration.min", n=5), lang)


def test_duration_samples():
    assert i18n.format_duration(300, "fr") == "5 min"
    assert i18n.format_duration(5400, "fr") == "1,5 h"
    assert i18n.format_duration(3600, "ru") == "1 ч"
    assert i18n.format_duration(60, "ar").replace(i18n.FSI, "").replace(i18n.PDI, "") == "دقيقة واحدة"
    assert i18n.format_duration(120, "ar").replace(i18n.FSI, "").replace(i18n.PDI, "") == "دقيقتان"
    assert i18n.format_duration(3600, "hi").replace(i18n.FSI, "").replace(i18n.PDI, "") == "1 घंटा"
    assert i18n.format_duration(7200, "hi") == "2 घंटे"
    assert i18n.format_duration(7200, "ja") == "2時間"


# ------------------------------------------------------------------------------------------ numbers and lists
@pytest.mark.parametrize(
    ("lang", "value", "expected"),
    [
        ("en", 1234567.891, "1,234,567.891"),
        ("fr", 1234567.891, "1 234 567,891"),
        ("de", 1234567.891, "1.234.567,891"),
        ("es", 1234, "1234"),
        ("es", 12345, "12.345"),
        ("pl", 1234, "1234"),
        ("it", 12345, "12.345"),
        ("hi", 1234567, "12,34,567"),
        ("hi", 1234, "1,234"),
        ("ru", 1234567, "1 234 567"),
        ("ar", -1234.5, "‎-1,234.5"),
        ("en", 0.0005, "0.001"),
        ("en", 2.5, "2.5"),
        ("en", -0.0004, "-0"),
        ("en", 1.005, "1.005"),
    ],
)
def test_format_number(lang, value, expected):
    assert i18n.format_number(value, lang) == expected


def test_format_number_decimals_and_grouping_and_rounding():
    assert i18n.format_number(1234.5, "en", 2) == "1,234.50"
    assert i18n.format_number(1234.5, "en", 0) == "1,235"  # half rounds away from zero like Intl
    assert i18n.format_number(2.5, "en", 0) == "3"
    assert i18n.format_number(-2.5, "en", 0) == "-3"
    assert i18n.format_number(1234.5, "fr", 1, grouping=False) == "1234,5"
    assert i18n.format_number(1.005, "en", 2) == "1.01"  # ICU rounds the shortest decimal, not the binary value
    assert i18n.format_number(10**30, "en", 0).startswith("1,000,000")
    assert math.isnan(float("nan")) and i18n.format_number(float("nan"), "en") == "NaN"
    assert i18n.format_number(float("inf"), "en") == "∞"


@pytest.mark.parametrize("lang", i18n.LANGUAGES)
def test_format_list_shapes(lang):
    assert i18n.format_list([], lang) == ""
    assert i18n.format_list(["A"], lang) == "A"
    pair = i18n.format_list(["A", "B"], lang)
    three = i18n.format_list(["A", "B", "C"], lang)
    four = i18n.format_list(["A", "B", "C", "D"], lang)
    assert pair.startswith("A") and pair.endswith("B")
    assert three.startswith("A") and three.endswith("C") and "B" in three
    assert four.startswith("A") and four.endswith("D")
    assert i18n.format_list(["A", "B", "C"], lang, "or") != three


def test_format_list_english_forms():
    assert i18n.format_list(["a", "b"], "en") == "a and b"
    assert i18n.format_list(["a", "b", "c"], "en") == "a, b, and c"
    assert i18n.format_list(["a", "b", "c"], "en", "or") == "a, b, or c"
    assert i18n.format_list(["a", "b", "c", "d"], "de") == "a, b, c und d"
    assert i18n.format_list(["a", "b", "c"], "ja") == "a、b、c"


def test_plural_category_basics():
    assert i18n.plural_category(1, "en") == "one"
    assert i18n.plural_category(1.0, "en") == "one"
    assert i18n.plural_category(1.5, "en") == "other"
    assert [i18n.plural_category(n, "ru") for n in (1, 2, 5, 11, 21, 22, 25)] == [
        "one",
        "few",
        "many",
        "many",
        "one",
        "few",
        "many",
    ]
    assert [i18n.plural_category(n, "ar") for n in (0, 1, 2, 3, 11, 100)] == [
        "zero",
        "one",
        "two",
        "few",
        "many",
        "other",
    ]
    assert i18n.plural_category(7, "ja") == "other"
    assert i18n.plural_category(-1, "en") == "one"


def test_english_catalog_is_json_utf8_with_final_newline():
    path = Path(i18n.__file__).parent / "frontend" / "i18n" / "common" / "en.json"
    assert path.read_text(encoding="utf-8").endswith("}\n")
