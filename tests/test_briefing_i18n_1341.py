"""Briefing as catalog messages (Issue #1341, lane L8): English is byte-identical, other languages render the same
messages from their own catalog, the stored briefing carries its message, and a pre-change stored state still loads.

Occupant impact if these fail: the daily briefing (notification, email, dashboard text) would show message keys or
English in a non-English home, or an existing stored briefing would not come back after the upgrade restart.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, time
from pathlib import Path
from types import SimpleNamespace

import pytest

from custom_components.climate_advisor import i18n
from custom_components.climate_advisor.briefing import BriefingText, generate_briefing, generate_briefing_msg
from custom_components.climate_advisor.classifier import DayClassification
from custom_components.climate_advisor.coordinator import (
    _briefing_display,
    _briefing_msg_wire,
    _restore_briefing_text,
)
from custom_components.climate_advisor.nat_vent_plan import (
    describe_close_timing,
    describe_nat_vent_cutoff_reason,
    describe_reopen_clause,
)
from tests.test_cross_day_restore_batch import _TODAY, _base, _restore

_NOW = datetime(2026, 5, 11, 6, 0, tzinfo=UTC)
_PKG = Path(__file__).resolve().parent.parent / "custom_components" / "climate_advisor"
_DAY_TYPES = ("hot", "warm", "mild", "cool", "cold")


@pytest.fixture(autouse=True)
def _strict_catalogs(frozen_now):
    frozen_now(_NOW)
    i18n.reset_catalogs()
    i18n.set_strict(True)
    i18n.load_catalogs()
    yield
    i18n.set_strict(None)
    i18n.reset_catalogs()


def _cls(day_type: str, **kw) -> DayClassification:
    base = dict(
        day_type=day_type,
        trend_direction="warming",
        trend_magnitude=12.0,
        today_high={"hot": 92.0, "warm": 80.0, "mild": 68.0, "cool": 52.0, "cold": 30.0}[day_type],
        today_low=50.0,
        tomorrow_high=75.0,
        tomorrow_low=50.0,
        hvac_mode={"hot": "cool", "warm": "off", "mild": "off", "cool": "heat", "cold": "heat"}[day_type],
        windows_recommended=day_type in ("warm", "mild"),
        forecast_windows_recommended=day_type in ("warm", "mild"),
        window_open_time=time(9, 0),
        window_close_time=time(11, 0),
        window_opportunity_morning=True,
        window_opportunity_evening=True,
        window_opportunity_morning_end=time(10, 0),
        window_opportunity_evening_start=time(19, 0),
        pre_condition=True,
    )
    base.update(kw)
    return DayClassification(**base)


def _briefing_kwargs(c, **kw):
    args = dict(
        classification=c,
        comfort_heat=70.0,
        comfort_cool=75.0,
        setback_heat=60.0,
        setback_cool=80.0,
        wake_time=time(6, 30),
        sleep_time=time(22, 30),
        now=_NOW,
        bedtime_setback_heat=66.0,
        bedtime_setback_cool=72.0,
        adaptive_thermal_active=True,
        learning_suggestions=["Try a wider band."],
    )
    args.update(kw)
    return args


def _all_variants():
    for day_type in _DAY_TYPES:
        for occupancy in ("home", "away", "vacation", "guest"):
            for unit in ("fahrenheit", "celsius"):
                yield day_type, occupancy, unit


@pytest.mark.parametrize(("day_type", "occupancy", "unit"), list(_all_variants()))
def test_every_variant_renders_in_strict_english_without_leaking_keys(day_type, occupancy, unit):
    """Strict mode raises on any key missing from briefing/en.json; no brace or key may reach the text."""
    c = _cls(day_type)
    text = generate_briefing(
        **_briefing_kwargs(c, occupancy_mode=occupancy, temp_unit=unit, fan_mode="whole_house_fan")
    )
    assert "briefing." not in text
    assert "{" not in text and "}" not in text
    assert text.startswith("\U0001f3e0 Your Home Climate Plan for Today\n" + "=" * 40 + "\n\n  Day Type: ")
    short = generate_briefing(**_briefing_kwargs(c, occupancy_mode=occupancy, temp_unit=unit), verbosity="tldr_only")
    assert short.startswith("  Day Type: ") and "Tomorrow:" in short


def test_celsius_reads_in_the_unit():
    text = generate_briefing(**_briefing_kwargs(_cls("cool"), temp_unit="celsius"))
    assert "Day Type: Cool (11\u00b0C)" in text  # 52 F


def test_grace_and_steps_sections_are_placed_by_the_catalog():
    c = _cls("warm")
    text = generate_briefing(
        **_briefing_kwargs(c, grace_active=True, grace_source="manual", manual_grace_seconds=300, timeline=None)
    )
    assert "One heads-up: you manually turned the HVAC back on earlier, so I'm in a 5 minutes hands-off window" in text
    # section order of the English layout: tldr, body, leaving, fresh air, grace, tonight, suggestions
    order = [
        text.index(s)
        for s in (
            "Day Type:",
            "If you head out",
            "If you want to",
            "One heads-up",
            "Looking ahead",
            "Suggestions Based",
        )
    ]
    assert order == sorted(order)


def test_wire_round_trip_renders_identically():
    msg = generate_briefing_msg(**_briefing_kwargs(_cls("warm")))
    wire = json.loads(json.dumps(msg.to_wire()))
    again = i18n.Msg.from_wire(wire)
    for lang in ("en", "de", "ar", "ja"):
        assert i18n.render(again, lang) == i18n.render(msg, lang)


def _marked_catalog(lang: str) -> None:
    """A stand-in translation: every briefing message wrapped in guillemets (a valid ICU message), so a rendered
    sentence that does not come from this language's catalog is visible as unwrapped text."""
    english = i18n._CATALOGS["en"]
    i18n._CATALOGS[lang] = {k: f"\u00ab{v}\u00bb" for k, v in english.items() if k.startswith("briefing.")}


def test_non_english_renders_every_sentence_from_its_own_catalog():
    _marked_catalog("de")
    msg = generate_briefing_msg(
        **_briefing_kwargs(_cls("warm"), temp_unit="celsius", grace_active=True, grace_source="manual")
    )
    text = i18n.render(msg, "de")
    assert text.count("\u00ab") == text.count("\u00bb") > 20
    # nothing outside a marked message is prose: strip the marked messages' own text by nesting depth
    depth, loose = 0, []
    for ch in text:
        if ch == "\u00ab":
            depth += 1
        elif ch == "\u00bb":
            depth -= 1
        elif depth == 0 and ch not in "\n":
            loose.append(ch)
    assert "".join(loose) == ""  # only the catalog's own layout newlines sit between messages
    assert "\u00b0C" in text and "\u00b0F" not in text


def test_translator_can_reorder_sections():
    _marked_catalog("de")
    i18n._CATALOGS["de"]["briefing.full.warm"] = "{tonight}\n{title}\n{tldr}"
    text = i18n.render(generate_briefing_msg(**_briefing_kwargs(_cls("warm"))), "de")
    assert text.index("Looking ahead") < text.index("Your Home Climate Plan") < text.index("Day Type:")
    assert "If you head out" not in text  # a translator may also drop a section


def test_message_times_follow_the_language():
    _marked_catalog("de")
    c = _cls("cold")
    text = i18n.render(generate_briefing_msg(**_briefing_kwargs(c)), "de")
    assert i18n.format_time(datetime(2026, 5, 11, 22, 30), "de") in text  # the bedtime row is a Time param


def test_string_helpers_keep_returning_english():
    assert describe_close_timing("7:00 PM", True) == "now"
    assert describe_close_timing("7:00 PM", False) == "7:00 PM"
    assert describe_nat_vent_cutoff_reason("comfort_floor") == "since indoor's already down to your comfort floor"
    assert describe_nat_vent_cutoff_reason(None) == "before outdoor air warms past indoor"
    assert describe_reopen_clause("11:00 PM", True).startswith("Later, once outdoor temps cool back down below indoor")


def test_briefing_text_is_english_str_and_rerenders():
    msg = generate_briefing_msg(**_briefing_kwargs(_cls("cool")))
    text = BriefingText(i18n.render_en(msg), msg)
    assert isinstance(text, str) and text == i18n.render_en(msg)
    _marked_catalog("fr")
    assert text.rendered("fr").startswith("\u00ab")
    assert text.rendered("fr") is text.rendered("fr")  # cached per language
    assert text.rendered("en") == str(text)
    assert BriefingText("plain").rendered("fr") == "plain"  # no message: shown as it is


def test_display_follows_hass_language_and_leaves_stub_strings_alone():
    msg = generate_briefing_msg(**_briefing_kwargs(_cls("cool")))
    text = BriefingText(i18n.render_en(msg), msg)
    _marked_catalog("de")
    de = SimpleNamespace(config=SimpleNamespace(language="de-CH"))
    en = SimpleNamespace(config=SimpleNamespace(language="en"))
    assert _briefing_display(de, text).startswith("\u00ab")
    assert _briefing_display(en, text) == str(text)
    assert _briefing_display(de, "stub text") == "stub text"
    assert _briefing_display(object(), text) == str(text)


def test_stored_wire_restores_and_a_malformed_one_falls_back_to_english():
    msg = generate_briefing_msg(**_briefing_kwargs(_cls("warm")))
    wire = json.loads(json.dumps(_briefing_msg_wire(BriefingText("english", msg))))
    restored = _restore_briefing_text("english", wire)
    assert isinstance(restored, BriefingText) and restored.msg == msg
    assert _briefing_msg_wire("plain") is None
    for bad in (None, "x", {}, {"key": "briefing.title", "params": {"a": {"$t": "nope"}}}):
        out = _restore_briefing_text("english", bad)
        assert out == "english" and getattr(out, "msg", None) is None


def test_restart_restores_a_pre_change_state_blob_unchanged():
    """A briefing_state written before #1341 has no message keys: the stored English comes back exactly."""
    legacy = {
        "sent_today": True,
        "last_text": "\U0001f3e0 Your Home Climate Plan for Today\nold full text",
        "last_text_short": "  Day Type: Warm (80\u00b0F)",
        "briefing_day_type": "warm",
        "briefing_today_high": 80.0,
    }
    coord = _restore(_base(_TODAY, briefing_state=legacy))
    assert coord._last_briefing == legacy["last_text"]
    assert coord._last_briefing_short == legacy["last_text_short"]
    assert getattr(coord._last_briefing, "msg", None) is None
    assert coord._briefing_sent_today is True


def test_restart_restores_the_message_and_rerenders_in_the_current_language():
    msg = generate_briefing_msg(**_briefing_kwargs(_cls("warm")))
    short_msg = generate_briefing_msg(**_briefing_kwargs(_cls("warm")), verbosity="tldr_only")
    stored = {
        "sent_today": True,
        "last_text": i18n.render_en(msg),
        "last_text_short": i18n.render_en(short_msg),
        "last_msg": json.loads(json.dumps(msg.to_wire())),
        "last_msg_short": json.loads(json.dumps(short_msg.to_wire())),
        "briefing_day_type": "warm",
    }
    coord = _restore(_base(_TODAY, briefing_state=stored))
    assert coord._last_briefing == stored["last_text"]
    assert coord._last_briefing.msg == msg and coord._last_briefing_short.msg == short_msg
    _marked_catalog("de")
    assert coord._last_briefing.rendered("de").startswith("\u00ab")


def test_every_catalog_key_is_used_by_the_code():
    keys = json.loads((_PKG / "frontend" / "i18n" / "briefing" / "en.json").read_text(encoding="utf-8"))
    source = "".join((_PKG / name).read_text(encoding="utf-8") for name in ("briefing.py", "nat_vent_plan.py"))
    dynamic = ("briefing.full.", "briefing.day_type.")
    unused = [k for k in keys if not k.startswith(dynamic) and f'"{k}"' not in source]
    assert not unused, unused
    assert {f"briefing.full.{d}" for d in _DAY_TYPES} | {"briefing.full.other"} <= set(keys)


def test_catalog_messages_never_hold_a_hardcoded_temperature_or_time():
    keys = json.loads((_PKG / "frontend" / "i18n" / "briefing" / "en.json").read_text(encoding="utf-8"))
    bad = [k for k, v in keys.items() if re.search(r"\d\s*\u00b0|\d:\d\d", v)]
    assert not bad, bad
