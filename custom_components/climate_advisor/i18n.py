"""Climate Advisor i18n engine: ICU-subset renderer, CLDR plurals and per-language formatters (Issue #1341).

Pure Python, synchronous, no ``homeassistant`` import and no I/O at render time. ``load_catalogs`` is the only
blocking call (read the JSON catalogs once, from an executor job); everything else works on the cached data.

What the person living in the home gets from this module: notifications, briefings and status text rendered in the
language Home Assistant is set to, with that language's plural forms, number separators, list wording and clock style.

Catalogs: ``frontend/i18n/<area>/<lang>.json`` (flat ``{"<area>.<key>": "<ICU message>"}``). A lookup falls back per
key, requested language then English. A key missing in English is a programming error: it raises in strict mode
(``set_strict(True)`` or ``CA_I18N_STRICT=1``, used by the tests) and logs one warning and returns the key in
production.

ICU subset (same semantics as FormatJS ``intl-messageformat`` 10.7.16, proven by the shared conformance fixture):

* ``{name}``; ``{n, plural, =0 {..} one {..} .. other {..}}`` with ``#``; ``{x, select, a {..} other {..}}``;
  ``{n, number}``, ``{n, number, ::percent}``, ``::precision-integer``, ``::.0``, ``::.00``.
* No tags, no date/time/selectordinal/offset, no nesting of plural/select deeper than two levels.
* Apostrophes follow FormatJS: ``''`` is a literal ``'``; a single ``'`` starts quoting only directly before
  ``{`` ``}`` ``<`` ``>`` (and ``#`` inside a plural body), the quote ends at the next single ``'``; any other ``'`` is
  literal. ``#`` is the plural number only directly in a plural body (literal inside a nested select).

Deviations from the first Stage 1 contract, both forced by FormatJS itself (the fixture is the arbiter):

* A plain ``{n}`` renders a numeric argument as JavaScript ``String(n)`` ("1234.5"), exactly like FormatJS; it is NOT
  locale formatted. Use ``{n, number}`` (or ``#`` in a plural) for locale formatting.
* ``::integer`` is silently ignored by FormatJS, so it is rejected here; ``::precision-integer`` is the integer
  skeleton.

For ``lang == "ar"`` every rendered argument (and ``#``) is wrapped in U+2068 ... U+2069 (first-strong isolates).

Times and dates are Python-owned tables modelled on ``Intl.DateTimeFormat`` (hour numeric, minute two-digit; short
month and day). Languages whose default is a 24-hour clock use it ("15:05"); en, ko, hi and ar use 12-hour with the
locale's day period ("3:05 PM", "오후 3:05", "3:05 pm", "3:05 م"). ICU's U+202F/U+00A0 inside times and dates are
normalised to a plain space. Number, percent and list separators keep the exact characters Intl emits (they are
compared byte for byte with Intl in the conformance fixture).
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, localcontext
from pathlib import Path
from typing import Any

_LOGGER = logging.getLogger(__name__)

LANGUAGES: tuple[str, ...] = (
    "en",
    "fr",
    "es",
    "de",
    "nl",
    "ja",
    "pt",
    "it",
    "pl",
    "uk",
    "ko",
    "hi",
    "ar",
    "ru",
    "zh-Hans",
    "tr",
)
AREAS: tuple[str, ...] = (
    "common",
    "notify",
    "status",
    "timeline",
    "learning",
    "night",
    "activity",
    "briefing",
    "dashboard",
    "settings",
    "errors",
    "ai",
)
DEFAULT_LANGUAGE = "en"
PLURAL_CATEGORIES = ("zero", "one", "two", "few", "many", "other")

FSI = "⁨"
PDI = "⁩"

# Per-language data below was generated from Node's Intl (ICU 76 / CLDR 46) by
# ``node tools/i18n_conformance_gen.mjs --tables`` and is committed as literals (no runtime Node).

_NUMBER_SYMBOLS: dict[str, tuple[str, str, str, str, int, bool]] = {
    # lang: (decimal, group, minus, percent pattern, min grouping digits, Indian grouping)
    "en": (".", ",", "-", "{}%", 1, False),
    "fr": (",", "\u202f", "-", "{}\u00a0%", 1, False),
    "es": (",", ".", "-", "{}\u00a0%", 2, False),
    "de": (",", ".", "-", "{}\u00a0%", 1, False),
    "nl": (",", ".", "-", "{}%", 1, False),
    "ja": (".", ",", "-", "{}%", 1, False),
    "pt": (",", ".", "-", "{}%", 1, False),
    "it": (",", ".", "-", "{}%", 2, False),
    "pl": (",", "\u00a0", "-", "{}%", 2, False),
    "uk": (",", "\u00a0", "-", "{}%", 1, False),
    "ko": (".", ",", "-", "{}%", 1, False),
    "hi": (".", ",", "-", "{}%", 1, True),
    "ar": (".", ",", "\u200e-", "{}\u200e%\u200e", 1, False),
    "ru": (",", "\u00a0", "-", "{}\u00a0%", 1, False),
    "zh-Hans": (".", ",", "-", "{}%", 1, False),
    "tr": (",", ".", "-", "%{}", 1, False),
}
_LIST_AND: dict[str, tuple[str, str, str, str]] = {
    # lang: (pair, start, middle, end) separators between items
    "en": (" and ", ", ", ", ", ", and "),
    "fr": (" et ", ", ", ", ", " et "),
    "es": (" y ", ", ", ", ", " y "),
    "de": (" und ", ", ", ", ", " und "),
    "nl": (" en ", ", ", ", ", " en "),
    "ja": ("、", "、", "、", "、"),
    "pt": (" e ", ", ", ", ", " e "),
    "it": (" e ", ", ", ", ", " e "),
    "pl": (" i ", ", ", ", ", " i "),
    "uk": (" і ", ", ", ", ", " і "),
    "ko": (" 및 ", ", ", ", ", " 및 "),
    "hi": (" और ", ", ", ", ", ", और "),
    "ar": (" و", " و", " و", " و"),
    "ru": (" и ", ", ", ", ", " и "),
    "zh-Hans": ("和", "、", "、", "和"),
    "tr": (" ve ", ", ", ", ", " ve "),
}
_LIST_OR: dict[str, tuple[str, str, str, str]] = {
    # lang: (pair, start, middle, end) separators between items
    "en": (" or ", ", ", ", ", ", or "),
    "fr": (" ou ", ", ", ", ", " ou "),
    "es": (" o ", ", ", ", ", " o "),
    "de": (" oder ", ", ", ", ", " oder "),
    "nl": (" of ", ", ", ", ", " of "),
    "ja": ("または", "、", "、", "、または"),
    "pt": (" ou ", ", ", ", ", " ou "),
    "it": (" o ", ", ", ", ", " o "),
    "pl": (" lub ", ", ", ", ", " lub "),
    "uk": (" або ", ", ", ", ", " або "),
    "ko": (" 또는 ", ", ", ", ", " 또는 "),
    "hi": (" या ", ", ", ", ", " या "),
    "ar": (" أو ", " أو ", " أو ", " أو "),
    "ru": (" или ", ", ", ", ", " или "),
    "zh-Hans": ("或", "、", "、", "或"),
    "tr": (" veya ", ", ", ", ", " veya "),
}
_TIME_FORMATS: dict[str, tuple[str, str, str]] = {
    # lang: (template, am, pm); template uses {h} (unpadded hour), {mm}, {p} (day period); am/pm "" = 24-hour
    "en": ("{h}:{mm} {p}", "AM", "PM"),
    "fr": ("{h}:{mm}", "", ""),
    "es": ("{h}:{mm}", "", ""),
    "de": ("{h}:{mm}", "", ""),
    "nl": ("{h}:{mm}", "", ""),
    "ja": ("{h}:{mm}", "", ""),
    "pt": ("{h}:{mm}", "", ""),
    "it": ("{h}:{mm}", "", ""),
    "pl": ("{h}:{mm}", "", ""),
    "uk": ("{h}:{mm}", "", ""),
    "ko": ("{p} {h}:{mm}", "오전", "오후"),
    "hi": ("{h}:{mm} {p}", "am", "pm"),
    "ar": ("{h}:{mm} {p}", "ص", "م"),
    "ru": ("{h}:{mm}", "", ""),
    "zh-Hans": ("{h}:{mm}", "", ""),
    "tr": ("{h}:{mm}", "", ""),
}
_DATE_FORMATS: dict[str, tuple[str, ...]] = {
    # lang: short "month day" template per month (Jan..Dec), {d} = day of month
    "en": (
        "Jan {d}",
        "Feb {d}",
        "Mar {d}",
        "Apr {d}",
        "May {d}",
        "Jun {d}",
        "Jul {d}",
        "Aug {d}",
        "Sep {d}",
        "Oct {d}",
        "Nov {d}",
        "Dec {d}",
    ),
    "fr": (
        "{d} janv.",
        "{d} févr.",
        "{d} mars",
        "{d} avr.",
        "{d} mai",
        "{d} juin",
        "{d} juil.",
        "{d} août",
        "{d} sept.",
        "{d} oct.",
        "{d} nov.",
        "{d} déc.",
    ),
    "es": (
        "{d} ene",
        "{d} feb",
        "{d} mar",
        "{d} abr",
        "{d} may",
        "{d} jun",
        "{d} jul",
        "{d} ago",
        "{d} sept",
        "{d} oct",
        "{d} nov",
        "{d} dic",
    ),
    "de": (
        "{d}. Jan.",
        "{d}. Feb.",
        "{d}. März",
        "{d}. Apr.",
        "{d}. Mai",
        "{d}. Juni",
        "{d}. Juli",
        "{d}. Aug.",
        "{d}. Sept.",
        "{d}. Okt.",
        "{d}. Nov.",
        "{d}. Dez.",
    ),
    "nl": (
        "{d} jan",
        "{d} feb",
        "{d} mrt",
        "{d} apr",
        "{d} mei",
        "{d} jun",
        "{d} jul",
        "{d} aug",
        "{d} sep",
        "{d} okt",
        "{d} nov",
        "{d} dec",
    ),
    "ja": (
        "1月{d}日",
        "2月{d}日",
        "3月{d}日",
        "4月{d}日",
        "5月{d}日",
        "6月{d}日",
        "7月{d}日",
        "8月{d}日",
        "9月{d}日",
        "10月{d}日",
        "11月{d}日",
        "12月{d}日",
    ),
    "pt": (
        "{d} de jan.",
        "{d} de fev.",
        "{d} de mar.",
        "{d} de abr.",
        "{d} de mai.",
        "{d} de jun.",
        "{d} de jul.",
        "{d} de ago.",
        "{d} de set.",
        "{d} de out.",
        "{d} de nov.",
        "{d} de dez.",
    ),
    "it": (
        "{d} gen",
        "{d} feb",
        "{d} mar",
        "{d} apr",
        "{d} mag",
        "{d} giu",
        "{d} lug",
        "{d} ago",
        "{d} set",
        "{d} ott",
        "{d} nov",
        "{d} dic",
    ),
    "pl": (
        "{d} sty",
        "{d} lut",
        "{d} mar",
        "{d} kwi",
        "{d} maj",
        "{d} cze",
        "{d} lip",
        "{d} sie",
        "{d} wrz",
        "{d} paź",
        "{d} lis",
        "{d} gru",
    ),
    "uk": (
        "{d} січ.",
        "{d} лют.",
        "{d} бер.",
        "{d} квіт.",
        "{d} трав.",
        "{d} черв.",
        "{d} лип.",
        "{d} серп.",
        "{d} вер.",
        "{d} жовт.",
        "{d} лист.",
        "{d} груд.",
    ),
    "ko": (
        "1월 {d}일",
        "2월 {d}일",
        "3월 {d}일",
        "4월 {d}일",
        "5월 {d}일",
        "6월 {d}일",
        "7월 {d}일",
        "8월 {d}일",
        "9월 {d}일",
        "10월 {d}일",
        "11월 {d}일",
        "12월 {d}일",
    ),
    "hi": (
        "{d} जन॰",
        "{d} फ़र॰",
        "{d} मार्च",
        "{d} अप्रैल",
        "{d} मई",
        "{d} जून",
        "{d} जुल॰",
        "{d} अग॰",
        "{d} सित॰",
        "{d} अक्तू॰",
        "{d} नव॰",
        "{d} दिस॰",
    ),
    "ar": (
        "{d} يناير",
        "{d} فبراير",
        "{d} مارس",
        "{d} أبريل",
        "{d} مايو",
        "{d} يونيو",
        "{d} يوليو",
        "{d} أغسطس",
        "{d} سبتمبر",
        "{d} أكتوبر",
        "{d} نوفمبر",
        "{d} ديسمبر",
    ),
    "ru": (
        "{d} янв.",
        "{d} февр.",
        "{d} мар.",
        "{d} апр.",
        "{d} мая",
        "{d} июн.",
        "{d} июл.",
        "{d} авг.",
        "{d} сент.",
        "{d} окт.",
        "{d} нояб.",
        "{d} дек.",
    ),
    "zh-Hans": (
        "1月{d}日",
        "2月{d}日",
        "3月{d}日",
        "4月{d}日",
        "5月{d}日",
        "6月{d}日",
        "7月{d}日",
        "8月{d}日",
        "9月{d}日",
        "10月{d}日",
        "11月{d}日",
        "12月{d}日",
    ),
    "tr": (
        "{d} Oca",
        "{d} Şub",
        "{d} Mar",
        "{d} Nis",
        "{d} May",
        "{d} Haz",
        "{d} Tem",
        "{d} Ağu",
        "{d} Eyl",
        "{d} Eki",
        "{d} Kas",
        "{d} Ara",
    ),
}

# CLDR cardinal plural categories per language (checked against Intl.PluralRules by tests/test_i18n_plural_drift.py).
PLURAL_CATEGORIES_BY_LANG: dict[str, tuple[str, ...]] = {
    "en": ("one", "other"),
    "fr": ("one", "many", "other"),
    "es": ("one", "many", "other"),
    "de": ("one", "other"),
    "nl": ("one", "other"),
    "ja": ("other",),
    "pt": ("one", "many", "other"),
    "it": ("one", "many", "other"),
    "pl": ("one", "few", "many", "other"),
    "uk": ("one", "few", "many", "other"),
    "ko": ("other",),
    "hi": ("one", "other"),
    "ar": ("zero", "one", "two", "few", "many", "other"),
    "ru": ("one", "few", "many", "other"),
    "zh-Hans": ("other",),
    "tr": ("one", "other"),
}

_HANT_MARKERS = frozenset({"hant", "tw", "hk", "mo"})


class I18nError(ValueError):
    """Base class for i18n failures (bad message syntax, missing key, bad parameter)."""


class MessageSyntaxError(I18nError):
    """The message is not valid in the supported ICU subset."""


class MissingKeyError(I18nError):
    """A catalog key is absent from English (a programming error)."""


# ------------------------------------------------------------------------------------------------ languages
def resolve_language(code: str | None) -> str:
    """Map any HA/browser language code to one of ``LANGUAGES`` (exact, case-insensitive, primary subtag, else en).

    ``zh``/``zh-CN``/``zh-SG``/``zh-Hans-*`` map to ``zh-Hans``; ``zh-TW``/``zh-HK``/``zh-Hant`` map to ``en`` for now.
    """
    if not code or not isinstance(code, str):
        return DEFAULT_LANGUAGE
    code = code.strip().replace("_", "-")
    if code in LANGUAGES:
        return code
    lowered = {lang.lower(): lang for lang in LANGUAGES}
    if code.lower() in lowered:
        return lowered[code.lower()]
    parts = code.lower().split("-")
    primary = parts[0]
    if primary == "zh":
        return DEFAULT_LANGUAGE if _HANT_MARKERS.intersection(parts[1:]) else "zh-Hans"
    if primary in lowered and "-" not in lowered[primary]:
        return lowered[primary]
    return DEFAULT_LANGUAGE


def hass_language(hass: Any) -> str:
    """The shipped language matching ``hass.config.language`` (en when unreadable)."""
    return resolve_language(getattr(getattr(hass, "config", None), "language", None))


# ------------------------------------------------------------------------------------------------ plurals
def _operands(x: float | int) -> tuple[int, int]:
    """Return (i, v): integer part and visible fraction-digit count, after Intl's default 3-digit rounding."""
    if isinstance(x, int):
        return abs(x), 0
    with localcontext() as ctx:
        ctx.prec = 80
        q = Decimal(repr(abs(float(x)))).quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)
        whole, _, frac = format(q, "f").partition(".")
    return int(whole), len(frac.rstrip("0"))


def _million(i: int, v: int) -> bool:
    return v == 0 and i != 0 and i % 1_000_000 == 0


def _p_other(i: int, v: int) -> str:
    return "other"


def _p_en(i: int, v: int) -> str:
    return "one" if i == 1 and v == 0 else "other"


def _p_fr(i: int, v: int) -> str:
    if i in (0, 1):
        return "one"
    return "many" if _million(i, v) else "other"


def _p_es_it(i: int, v: int) -> str:
    if i == 1 and v == 0:
        return "one"
    return "many" if _million(i, v) else "other"


def _p_pt(i: int, v: int) -> str:
    if i in (0, 1):
        return "one"
    return "many" if _million(i, v) else "other"


def _p_hi(i: int, v: int) -> str:
    return "one" if i == 0 or (i == 1 and v == 0) else "other"


def _p_east_slavic(i: int, v: int) -> str:
    if v != 0:
        return "other"
    m10, m100 = i % 10, i % 100
    if m10 == 1 and m100 != 11:
        return "one"
    if 2 <= m10 <= 4 and not 12 <= m100 <= 14:
        return "few"
    return "many"


def _p_pl(i: int, v: int) -> str:
    if v != 0:
        return "other"
    m10, m100 = i % 10, i % 100
    if i == 1:
        return "one"
    if 2 <= m10 <= 4 and not 12 <= m100 <= 14:
        return "few"
    if m10 in (0, 1) or 5 <= m10 <= 9 or 12 <= m100 <= 14:
        return "many"
    return "other"


def _p_ar(i: int, v: int) -> str:
    if v != 0:
        return "other"
    if i in (0, 1, 2):
        return ("zero", "one", "two")[i]
    m100 = i % 100
    if 3 <= m100 <= 10:
        return "few"
    return "many" if 11 <= m100 <= 99 else "other"


_PLURAL_RULES = {
    "en": _p_en,
    "de": _p_en,
    "nl": _p_en,
    "tr": _p_en,
    "fr": _p_fr,
    "es": _p_es_it,
    "it": _p_es_it,
    "pt": _p_pt,
    "hi": _p_hi,
    "ru": _p_east_slavic,
    "uk": _p_east_slavic,
    "pl": _p_pl,
    "ar": _p_ar,
    "ja": _p_other,
    "ko": _p_other,
    "zh-Hans": _p_other,
}


def plural_category(n: float | int, lang: str) -> str:
    """CLDR cardinal plural category of ``n`` in ``lang`` (same operands Intl.PluralRules uses)."""
    i, v = _operands(n)
    return _PLURAL_RULES[resolve_language(lang)](i, v)


# ------------------------------------------------------------------------------------------------ numbers
def js_str(x: Any) -> str:
    """JavaScript ``String(x)`` for the values a plain ``{name}`` argument can carry."""
    if isinstance(x, bool):
        return "true" if x else "false"
    if isinstance(x, int):
        return str(x)
    if isinstance(x, float):
        if x != x:
            return "NaN"
        if x in (float("inf"), float("-inf")):
            return "Infinity" if x > 0 else "-Infinity"
        if x == int(x) and abs(x) < 1e21:
            return str(int(x))
        if 1e-7 <= abs(x) < 1e21:
            with localcontext() as ctx:
                ctx.prec = 80
                return format(Decimal(repr(x)), "f")
        return repr(x)
    return str(x)


def _group(digits: str, sep: str, min_group: int, indian: bool) -> str:
    if len(digits) < 3 + min_group:
        return digits
    head, tail = digits[:-3], digits[-3:]
    size = 2 if indian else 3
    chunks = []
    while head:
        chunks.append(head[-size:])
        head = head[:-size]
    return sep.join([*reversed(chunks), tail])


def _format_decimal(
    value: float | int, lang: str, min_frac: int, max_frac: int, *, grouping: bool = True, percent: bool = False
) -> str:
    decimal, group, minus, pct, min_group, indian = _NUMBER_SYMBOLS[lang]
    if isinstance(value, float) and (value != value or value in (float("inf"), float("-inf"))):
        return "NaN" if value != value else (minus if value < 0 else "") + "∞"
    with localcontext() as ctx:
        ctx.prec = 80
        d = Decimal(value) if isinstance(value, int) else Decimal(repr(value))
        if percent:
            d = d.scaleb(2)
        negative = d.is_signed()
        q = abs(d).quantize(Decimal(1).scaleb(-max_frac), rounding=ROUND_HALF_UP)
        text = format(q, "f")
    whole, _, frac = text.partition(".")
    frac = frac.rstrip("0")
    if len(frac) < min_frac:
        frac = frac.ljust(min_frac, "0")
    if grouping:
        whole = _group(whole, group, min_group, indian)
    body = whole + (decimal + frac if frac else "")
    if percent:
        body = pct.replace("{}", body)
    return (minus if negative else "") + body


def format_number(x: float | int, lang: str, decimals: int | None = None, *, grouping: bool = True) -> str:
    """Locale-format ``x`` like ``Intl.NumberFormat``; ``decimals`` None = up to 3 fraction digits."""
    lang = resolve_language(lang)
    lo, hi = (0, 3) if decimals is None else (decimals, decimals)
    return _format_decimal(x, lang, lo, hi, grouping=grouping)


def format_list(items: Sequence[str], lang: str, style: str = "and") -> str:
    """Join already-rendered ``items`` like ``Intl.ListFormat`` (type conjunction for "and", disjunction for "or")."""
    lang = resolve_language(lang)
    items = [str(i) for i in items]
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    pair, start, middle, end = (_LIST_OR if style == "or" else _LIST_AND)[lang]
    if len(items) == 2:
        return items[0] + pair + items[1]
    out = items[0] + start + items[1]
    for item in items[2:-1]:
        out += middle + item
    return out + end + items[-1]


# ------------------------------------------------------------------------------------------------ time and date
def format_time(dt: _dt.datetime | _dt.time, lang: str) -> str:
    """Clock time as the language's default style: ``3:05 PM`` in English (byte-identical to ``%-I:%M %p``)."""
    template, am, pm = _TIME_FORMATS[resolve_language(lang)]
    hour = dt.hour
    if am:
        shown, period = hour % 12 or 12, (am if hour < 12 else pm)
    else:
        shown, period = hour, ""
    return template.replace("{h}", str(shown)).replace("{mm}", f"{dt.minute:02d}").replace("{p}", period)


def format_date(dt: _dt.date, lang: str) -> str:
    """Short month and day (``Nov 14`` / ``14 nov.`` / ``11月14日``)."""
    return _DATE_FORMATS[resolve_language(lang)][dt.month - 1].replace("{d}", str(dt.day))


def format_duration(seconds: float, lang: str) -> str:
    """A duration as ``N min`` / ``Nh`` / ``N.Nh`` (English form matches the existing grace-timer text).

    Rendered from the ``common.duration.*`` catalog messages, so every language is a translated message.
    """
    minutes = max(1, round(seconds / 60))
    if minutes >= 60 and minutes % 60 == 0:
        msg = Msg("common.duration.hours", n=minutes // 60)
    elif minutes >= 60:
        msg = Msg("common.duration.hours_frac", n=float(f"{minutes / 60:.1f}"))
    else:
        msg = Msg("common.duration.min", n=minutes)
    return render(msg, lang)


# ------------------------------------------------------------------------------------------------ ICU parser
# AST nodes: ("lit", text) ("arg", name) ("num", name, style) ("pound",) ("plural", name, {selector: nodes})
# ("select", name, {selector: nodes}); style is None or (kind, min_frac, max_frac) with kind "decimal"|"percent".
_NUM_SKELETONS = {
    "percent": ("percent", 0, 0),
    "precision-integer": ("decimal", 0, 0),
}
_FRACTION_SKELETON = re.compile(r"\.(0+)")
_IDENT = re.compile(r"[A-Za-z0-9_]+")
_WS = " \t\r\n"
MAX_NESTING = 2


def _skeleton(text: str) -> tuple[str, int, int]:
    if text in _NUM_SKELETONS:
        return _NUM_SKELETONS[text]
    m = _FRACTION_SKELETON.fullmatch(text)
    if m:
        return ("decimal", len(m.group(1)), len(m.group(1)))
    raise MessageSyntaxError(f"unsupported number skeleton ::{text}")


class _Parser:
    def __init__(self, text: str) -> None:
        self.s = text
        self.n = len(text)
        self.i = 0

    def fail(self, why: str) -> MessageSyntaxError:
        return MessageSyntaxError(f"{why} at {self.i} in {self.s!r}")

    def ws(self) -> None:
        while self.i < self.n and self.s[self.i] in _WS:
            self.i += 1

    def parse(self) -> tuple:
        nodes = self.message(0, None, 0)
        if self.i < self.n:
            raise self.fail("unexpected character")
        return tuple(nodes)

    def message(self, nesting: int, parent: str | None, depth: int) -> list:
        out: list = []
        while self.i < self.n:
            ch = self.s[self.i]
            if ch == "{":
                out.append(self.argument(nesting, depth))
            elif ch == "}" and nesting > 0:
                break
            elif ch == "#" and parent == "plural":
                out.append(("pound",))
                self.i += 1
            elif ch == "<" and self.i + 1 < self.n and (self.s[self.i + 1].isalpha() or self.s[self.i + 1] == "/"):
                raise self.fail("tags are not supported")
            else:
                start = self.i
                text = self.literal(nesting, parent)
                if self.i == start:
                    raise self.fail("parser made no progress")
                out.append(("lit", text))
        merged: list = []
        for node in out:
            if node[0] == "lit" and merged and merged[-1][0] == "lit":
                merged[-1] = ("lit", merged[-1][1] + node[1])
            else:
                merged.append(node)
        return merged

    def literal(self, nesting: int, parent: str | None) -> str:
        buf: list[str] = []
        while True:
            quoted = self.try_quote(parent)
            if quoted is not None:
                buf.append(quoted)
                continue
            if self.i >= self.n:
                break
            ch = self.s[self.i]
            if ch == "<":
                nxt = self.s[self.i + 1] if self.i + 1 < self.n else ""
                if nxt and (nxt.isalpha() or nxt == "/"):
                    break
                self.i += 1
                buf.append("<")
                continue
            if ch == "{" or (ch == "#" and parent == "plural") or (ch == "}" and nesting > 0):
                break
            self.i += 1
            buf.append(ch)
        return "".join(buf)

    def try_quote(self, parent: str | None) -> str | None:
        """FormatJS ``tryParseQuote``."""
        if self.i >= self.n or self.s[self.i] != "'":
            return None
        nxt = self.s[self.i + 1] if self.i + 1 < self.n else ""
        if nxt == "'":
            self.i += 2
            return "'"
        if not (nxt in ("{", "}", "<", ">") or (nxt == "#" and parent == "plural")):
            return None
        self.i += 1
        chars = [self.s[self.i]]
        self.i += 1
        while self.i < self.n:
            c = self.s[self.i]
            if c == "'":
                if self.i + 1 < self.n and self.s[self.i + 1] == "'":
                    chars.append("'")
                    self.i += 1
                else:
                    self.i += 1
                    break
            else:
                chars.append(c)
            self.i += 1
        return "".join(chars)

    def ident(self) -> str:
        m = _IDENT.match(self.s, self.i)
        if not m:
            raise self.fail("expected an identifier")
        self.i = m.end()
        return m.group(0)

    def expect(self, ch: str) -> None:
        if self.i >= self.n or self.s[self.i] != ch:
            raise self.fail(f"expected {ch!r}")
        self.i += 1

    def argument(self, nesting: int, depth: int) -> tuple:
        self.i += 1  # {
        self.ws()
        name = self.ident()
        self.ws()
        if self.i >= self.n:
            raise self.fail("unterminated argument")
        if self.s[self.i] == "}":
            self.i += 1
            return ("arg", name)
        self.expect(",")
        self.ws()
        kind = self.ident()
        self.ws()
        if kind == "number":
            return self.number(name)
        if kind in ("plural", "select"):
            if depth + 1 > MAX_NESTING:
                raise self.fail(f"plural/select nested deeper than {MAX_NESTING}")
            return self.options(kind, name, nesting, depth)
        raise self.fail(f"unsupported argument type {kind!r}")

    def number(self, name: str) -> tuple:
        if self.i < self.n and self.s[self.i] == "}":
            self.i += 1
            return ("num", name, None)
        self.expect(",")
        self.ws()
        end = self.s.find("}", self.i)
        if end < 0:
            raise self.fail("unterminated number style")
        style = self.s[self.i : end].strip()
        if not style.startswith("::"):
            raise self.fail("only ::skeleton number styles are supported")
        node = ("num", name, _skeleton(style[2:].strip()))
        self.i = end + 1
        return node

    def options(self, kind: str, name: str, nesting: int, depth: int) -> tuple:
        self.expect(",")
        self.ws()
        if kind == "plural" and self.s.startswith("offset:", self.i):
            raise self.fail("plural offset is not supported")
        opts: dict[str, list] = {}
        while True:
            self.ws()
            if self.i >= self.n:
                raise self.fail("unterminated argument")
            if self.s[self.i] == "}":
                break
            if kind == "plural" and self.s[self.i] == "=":
                m = re.compile(r"=\d+").match(self.s, self.i)
                if not m:
                    raise self.fail("bad exact plural selector")
                selector = m.group(0)
                self.i = m.end()
            else:
                selector = self.ident()
            if selector in opts:
                raise self.fail(f"duplicate selector {selector!r}")
            self.ws()
            self.expect("{")
            body = self.message(nesting + 1, kind, depth + 1)
            self.expect("}")
            opts[selector] = body
        self.i += 1  # closing } of the argument
        if "other" not in opts:
            raise self.fail(f"{kind} requires an 'other' clause")
        return (kind, name, opts)


_PARSE_CACHE: dict[str, tuple] = {}


def parse_message(pattern: str) -> tuple:
    """Parse an ICU-subset message into an AST (raises ``MessageSyntaxError``). Results are cached."""
    ast = _PARSE_CACHE.get(pattern)
    if ast is None:
        ast = _Parser(pattern).parse()
        if len(_PARSE_CACHE) > 4096:
            _PARSE_CACHE.clear()
        _PARSE_CACHE[pattern] = ast
    return ast


# ------------------------------------------------------------------------------------------------ ICU renderer
def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _render_nodes(nodes: Sequence[tuple], lang: str, params: Mapping[str, Any], pound: float | int | None) -> str:
    isolate = lang == "ar"
    out: list[str] = []

    def put(text: str) -> None:
        out.append(f"{FSI}{text}{PDI}" if isolate else text)

    for node in nodes:
        kind = node[0]
        if kind == "lit":
            out.append(node[1])
            continue
        if kind == "pound":
            if pound is not None:
                put(_format_decimal(pound, lang, 0, 3))
            else:
                out.append("#")
            continue
        name = node[1]
        if name not in params:
            raise I18nError(f"missing value for argument {name!r}")
        value = params[name]
        if kind == "arg":
            put(js_str(value))
        elif kind == "num":
            if not _is_number(value):
                raise I18nError(f"argument {name!r} must be a number for {{{name}, number}}")
            style = node[2]
            if style is None:
                put(_format_decimal(value, lang, 0, 3))
            else:
                put(_format_decimal(value, lang, style[1], style[2], percent=style[0] == "percent"))
        elif kind == "plural":
            if not _is_number(value):
                raise I18nError(f"argument {name!r} must be a number for plural")
            opts = node[2]
            body = opts.get("=" + js_str(value))
            if body is None:
                body = opts.get(plural_category(value, lang), opts["other"])
            out.append(_render_nodes(body, lang, params, value))
        elif kind == "select":
            if isinstance(value, bool) or not isinstance(value, (str, int, float)):
                raise I18nError(f"argument {name!r} must be text for select")
            opts = node[2]
            out.append(_render_nodes(opts.get(js_str(value), opts["other"]), lang, params, None))
    return "".join(out)


def format_message(pattern: str, lang: str, params: Mapping[str, Any] | None = None) -> str:
    """Render one ICU-subset ``pattern`` in ``lang``. ``params`` may hold plain values and the typed params below."""
    lang = resolve_language(lang)
    plain = {k: _to_plain(v, lang) for k, v in (params or {}).items()}
    return _render_nodes(parse_message(pattern), lang, plain, None)


# ------------------------------------------------------------------------------------------------ typed params
def _num_or_text(v: Any) -> Any:
    if isinstance(v, bool) or not isinstance(v, (str, int, float)):
        raise I18nError(f"unsupported parameter value {v!r}")
    return v


@dataclass(frozen=True)
class Temp:
    """An internal-Fahrenheit temperature shown in ``unit`` (wire ``{"$t":"temp","f","unit","d"}``)."""

    f: float
    unit: str
    decimals: int = 0

    def to_wire(self) -> dict:
        return {"$t": "temp", "f": self.f, "unit": self.unit, "d": self.decimals}


@dataclass(frozen=True)
class TempDelta:
    """A temperature difference (scale-only conversion)."""

    f: float
    unit: str
    decimals: int = 0

    def to_wire(self) -> dict:
        return {"$t": "tdelta", "f": self.f, "unit": self.unit, "d": self.decimals}


@dataclass(frozen=True)
class Time:
    """A wall-clock time (the datetime's own local fields are shown)."""

    dt: _dt.datetime

    def to_wire(self) -> dict:
        return {"$t": "time", "ts": self.dt.isoformat()}


@dataclass(frozen=True)
class Date:
    """A calendar date (short month and day)."""

    dt: _dt.datetime

    def to_wire(self) -> dict:
        return {"$t": "date", "ts": self.dt.isoformat()}


@dataclass(frozen=True)
class Duration:
    seconds: float

    def to_wire(self) -> dict:
        return {"$t": "dur", "s": self.seconds}


@dataclass(frozen=True)
class ListOf:
    items: tuple
    style: str = "and"

    def __init__(self, items: Sequence[Any], style: str = "and") -> None:
        object.__setattr__(self, "items", tuple(items))
        object.__setattr__(self, "style", style)

    def to_wire(self) -> dict:
        return {"$t": "list", "items": [param_to_wire(i) for i in self.items], "style": self.style}


@dataclass(frozen=True)
class Code:
    """An enum value rendered through its own catalog key (e.g. ``common.mode.cool``)."""

    key: str

    def to_wire(self) -> dict:
        return {"$t": "code", "key": self.key}


class Msg:
    """A catalog key plus typed parameters; renders in any language and round-trips through JSON."""

    __slots__ = ("key", "params")

    def __init__(self, key: str, **params: Any) -> None:
        self.key = key
        self.params = params

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Msg) and self.key == other.key and self.params == other.params

    def __hash__(self) -> int:
        return hash((self.key, tuple(sorted(self.params))))

    def __repr__(self) -> str:
        args = "".join(f", {k}={v!r}" for k, v in self.params.items())
        return f"Msg({self.key!r}{args})"

    def to_wire(self) -> dict:
        return {"key": self.key, "params": {k: param_to_wire(v) for k, v in self.params.items()}}

    @classmethod
    def from_wire(cls, data: Mapping[str, Any]) -> Msg:
        msg = cls(str(data["key"]))
        msg.params = {k: param_from_wire(v) for k, v in (data.get("params") or {}).items()}
        return msg


def param_to_wire(v: Any) -> Any:
    """Python param to the JSON wire form (plain str/int/float pass through)."""
    if isinstance(v, Msg):
        return {"$t": "msg", **v.to_wire()}
    if hasattr(v, "to_wire"):
        return v.to_wire()
    return _num_or_text(v)


def param_from_wire(v: Any) -> Any:
    """Inverse of ``param_to_wire``."""
    if not isinstance(v, Mapping):
        return _num_or_text(v)
    kind = v.get("$t")
    if kind == "temp":
        return Temp(v["f"], v["unit"], v.get("d", 0))
    if kind == "tdelta":
        return TempDelta(v["f"], v["unit"], v.get("d", 0))
    if kind == "time":
        return Time(_dt.datetime.fromisoformat(v["ts"]))
    if kind == "date":
        return Date(_dt.datetime.fromisoformat(v["ts"]))
    if kind == "dur":
        return Duration(v["s"])
    if kind == "list":
        return ListOf([param_from_wire(i) for i in v["items"]], v.get("style", "and"))
    if kind == "msg":
        return Msg.from_wire(v)
    if kind == "code":
        return Code(v["key"])
    raise I18nError(f"unknown typed parameter {kind!r}")


def _to_plain(v: Any, lang: str) -> Any:
    """Reduce a param to what the ICU engine takes: str, int or float."""
    if isinstance(v, (Temp, TempDelta)):
        from .temperature import format_temp, format_temp_delta  # noqa: PLC0415 - temperature imports this module lazily too

        fmt = format_temp if isinstance(v, Temp) else format_temp_delta
        return fmt(v.f, v.unit, v.decimals, lang=lang)
    if isinstance(v, Time):
        return format_time(v.dt, lang)
    if isinstance(v, Date):
        return format_date(v.dt, lang)
    if isinstance(v, Duration):
        return format_duration(v.seconds, lang)
    if isinstance(v, ListOf):
        texts = []
        for item in v.items:
            plain = _to_plain(item, lang)
            texts.append(format_number(plain, lang) if _is_number(plain) else str(plain))
        return format_list(texts, lang, v.style)
    if isinstance(v, Msg):
        return _render_msg(v, lang)
    if isinstance(v, Code):
        return _render_msg(Msg(v.key), lang)
    return _num_or_text(v)


# ------------------------------------------------------------------------------------------------ catalogs
_CATALOGS: dict[str, dict[str, str]] = {}
_LOADED_FROM: Path | None = None
_STRICT_OVERRIDE: bool | None = None
_WARNED: set[str] = set()


def default_i18n_dir() -> Path:
    return Path(__file__).parent / "frontend" / "i18n"


def load_catalogs(frontend_dir: Path | None = None) -> None:
    """Read every area/language catalog into memory (blocking: call once from an executor job; idempotent)."""
    global _CATALOGS, _LOADED_FROM  # noqa: PLW0603
    i18n_dir = (Path(frontend_dir) / "i18n") if frontend_dir is not None else default_i18n_dir()
    if i18n_dir == _LOADED_FROM:
        return
    index = json.loads((i18n_dir / "index.json").read_text(encoding="utf-8"))
    if tuple(index.get("languages", ())) != LANGUAGES or tuple(index.get("areas", ())) != AREAS:
        raise I18nError("frontend/i18n/index.json disagrees with i18n.LANGUAGES / i18n.AREAS")
    catalogs: dict[str, dict[str, str]] = {lang: {} for lang in LANGUAGES}
    for area in AREAS:
        for lang in LANGUAGES:
            data = json.loads((i18n_dir / area / f"{lang}.json").read_text(encoding="utf-8"))
            for key, value in data.items():
                if not key.startswith(area + ".") or not isinstance(value, str):
                    raise I18nError(f"{area}/{lang}.json: bad entry {key!r}")
                catalogs[lang][key] = value
    _CATALOGS = catalogs
    _LOADED_FROM = i18n_dir


def catalogs_loaded() -> bool:
    return _LOADED_FROM is not None


def reset_catalogs() -> None:
    """Forget the cached catalogs (tests)."""
    global _CATALOGS, _LOADED_FROM  # noqa: PLW0603
    _CATALOGS = {}
    _LOADED_FROM = None


def set_strict(flag: bool | None) -> None:
    """Force strict mode on/off (None = follow ``CA_I18N_STRICT``). Strict raises on a key missing from English."""
    global _STRICT_OVERRIDE  # noqa: PLW0603
    _STRICT_OVERRIDE = flag


def _strict() -> bool:
    if _STRICT_OVERRIDE is not None:
        return _STRICT_OVERRIDE
    return os.environ.get("CA_I18N_STRICT") == "1"


def _warn_once(what: str) -> None:
    if what not in _WARNED:
        _WARNED.add(what)
        _LOGGER.warning("i18n: %s", what)


def get_message(key: str, lang: str) -> tuple[str, str] | None:
    """The catalog pattern for ``key`` and the language it came from (per-key fallback to English)."""
    lang = resolve_language(lang)
    for candidate in (lang, DEFAULT_LANGUAGE):
        pattern = _CATALOGS.get(candidate, {}).get(key)
        if pattern is not None:
            return pattern, candidate
    return None


def _render_msg(msg: Msg, lang: str) -> str:
    found = get_message(msg.key, lang)
    if found is None:
        if _strict():
            raise MissingKeyError(msg.key)
        _warn_once(f"missing catalog key {msg.key!r}" + ("" if catalogs_loaded() else " (catalogs not loaded)"))
        return msg.key
    pattern, _source = found
    try:
        return format_message(pattern, lang, msg.params)
    except I18nError:
        if _strict():
            raise
        _warn_once(f"cannot render {msg.key!r} in {lang}")
        english = get_message(msg.key, DEFAULT_LANGUAGE)
        if lang != DEFAULT_LANGUAGE and english is not None:
            try:
                return format_message(english[0], DEFAULT_LANGUAGE, msg.params)
            except I18nError:
                pass
        return msg.key


def render(msg: Msg | Code | str, lang: str) -> str:
    """Render ``msg`` in ``lang``; a plain ``str`` passes through unchanged (gradual migration)."""
    if isinstance(msg, str):
        return msg
    if isinstance(msg, Code):
        msg = Msg(msg.key)
    return _render_msg(msg, resolve_language(lang))


def render_en(msg: Msg | str) -> str:
    """English rendering for logs, stored reasons, GitHub text and AI input."""
    return render(msg, DEFAULT_LANGUAGE)


# ------------------------------------------------------------------------------------------------ en-XA pseudo-locale
# Issue #1341 guard: a generated test locale, never shipped. Every letter of the literal text becomes an accented
# look-alike, the text grows by about 35% (a tilde pad), and the whole message is wrapped in brackets, while ICU
# syntax (arguments, plural/select selectors, ``#``, quoted braces, number styles) is left untouched. Any plain ASCII
# vowel left in a rendered en-XA string therefore came from outside the catalog: a hard-coded English bypass.
PSEUDO_EXPANSION = 0.35
_PSEUDO_MAP = str.maketrans(
    "AEIOUYaeiouyCcDdNnSsZz",
    "ÀÉÎÕÛÝàéîõûýÇçÐđÑñŠšŽž",
)


def pseudo(text: str) -> str:
    """The en-XA rendering of an ICU-subset ``text``: accented, ~35% longer, bracketed; syntax preserved."""
    letters = 0

    def lit(chunk: str) -> str:
        nonlocal letters
        letters += sum(1 for c in chunk if c.isalpha())
        return chunk.translate(_PSEUDO_MAP)

    def skip_to(s: str, i: int, stops: str) -> int:
        while i < len(s) and s[i] not in stops:
            i += 1
        return i

    def quoted(s: str, i: int) -> int | None:
        """End index of a ``'{...}'`` quote starting at ``i`` (kept verbatim), or ``None``."""
        if s[i] != "'" or i + 1 >= len(s) or s[i + 1] not in "{}#<":
            return None
        j = i + 2
        while j < len(s):
            if s[j] == "'":
                if j + 1 < len(s) and s[j + 1] == "'":
                    j += 2
                    continue
                return j + 1
            j += 1
        return len(s)

    def message(s: str, i: int, nested: bool) -> tuple[str, int]:
        out: list[str] = []
        while i < len(s):
            ch = s[i]
            if ch == "}" and nested:
                break
            if ch == "{":
                chunk, i = argument(s, i)
                out.append(chunk)
                continue
            q = quoted(s, i)
            if q is not None:
                out.append(s[i:q])
                i = q
                continue
            if ch == "#":
                out.append(ch)
                i += 1
                continue
            j = i + 1
            while j < len(s) and s[j] not in "{}#'":
                j += 1
            out.append(lit(s[i:j]))
            i = j
        return "".join(out), i

    def argument(s: str, i: int) -> tuple[str, int]:
        end = skip_to(s, i, ",}")
        if end >= len(s) or s[end] == "}":
            return s[i : end + 1], end + 1
        type_end = skip_to(s, end + 1, ",}")
        kind = s[end + 1 : type_end].strip()
        if kind not in ("plural", "select", "selectordinal") or type_end >= len(s) or s[type_end] == "}":
            close = skip_to(s, type_end, "}")
            return s[i : close + 1], close + 1
        out = [s[i : type_end + 1]]
        j = type_end + 1
        while j < len(s) and s[j] != "}":
            sel_end = skip_to(s, j, "{}")
            out.append(s[j:sel_end])
            if sel_end >= len(s) or s[sel_end] == "}":
                j = sel_end
                break
            body, j = message(s, sel_end + 1, True)
            out.append("{" + body + "}")
            j += 1  # the closing brace of the option
        out.append("}")
        return "".join(out), j + 1

    body, _end = message(text, 0, False)
    pad = "~" * -(-int(letters * PSEUDO_EXPANSION * 100) // 100) if letters else ""
    return f"[{body}{pad}]"
