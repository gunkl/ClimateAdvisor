"""#1341 lane L6: learning suggestions and the night strategy render from the ``learning`` / ``night`` catalogs.

English output is pinned byte-for-byte against the pre-conversion text; a second language is exercised through a
synthetic catalog (the real translations arrive in a later phase) to prove params are substituted and nothing is
concatenated in code.
"""

from __future__ import annotations

import dataclasses
import json
import re
from datetime import UTC, datetime
from pathlib import Path

import pytest

from custom_components.climate_advisor import i18n
from custom_components.climate_advisor import night_strategy as ns
from custom_components.climate_advisor.learning import LearningEngine, LearningState
from tests.test_learning_suggestions import _seed_window_compliance_records
from tests.test_night_strategy import cold_night, warm_night

PKG = Path(__file__).resolve().parent.parent / "custom_components" / "climate_advisor"
CATALOG = PKG / "frontend" / "i18n"


def _en(area: str) -> dict[str, str]:
    return json.loads((CATALOG / area / "en.json").read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def _catalogs():
    if not i18n.catalogs_loaded():
        i18n.load_catalogs()


@pytest.fixture
def fr_catalog(monkeypatch):
    """A synthetic ``fr`` catalog for the keys a test names (the structure is what is under test)."""

    def install(entries: dict[str, str]) -> None:
        cats = {lang: dict(c) for lang, c in i18n._CATALOGS.items()}
        cats.setdefault("fr", {}).update(entries)
        monkeypatch.setattr(i18n, "_CATALOGS", cats)

    return install


def _engine(tmp_path: Path) -> LearningEngine:
    e = LearningEngine(tmp_path)
    e._state = LearningState()
    return e


def _rec(**kw) -> dict:
    base = {
        "date": "2026-03-01",
        "day_type": "warm",
        "windows_recommended": False,
        "windows_opened": False,
        "occupancy_mode": "home",
        "hvac_runtime_minutes": 60.0,
        "manual_overrides": 0,
        "override_details": [],
        "door_window_pause_events": 0,
        "door_pause_by_sensor": {},
        "comfort_violations_minutes": 0.0,
        "occupancy_away_minutes": 0.0,
    }
    base.update(kw)
    return base


# ---------------------------------------------------------------------------------------------- catalog hygiene
@pytest.mark.parametrize(("module", "area"), [("learning.py", "learning"), ("night_strategy.py", "night")])
def test_every_key_the_module_names_is_in_the_english_catalog(module: str, area: str) -> None:
    src = (PKG / module).read_text(encoding="utf-8")
    used = set(re.findall(rf'"({area}\.[a-z0-9_.]+[a-z0-9])"', src))
    assert used, "the module must name catalog keys"
    cat = _en(area)
    # the sentence keys are derived from the sentence table, the rest are literals
    derived = {ns.sentence_msg_key(k) for k in ns.NIGHT_SENTENCES} if area == "night" else set()
    assert used - derived <= set(cat), sorted(used - derived - set(cat))
    assert derived <= set(cat)


def test_night_sentence_templates_equal_their_catalog_entries() -> None:
    cat = _en("night")
    for key, tmpl in ns.NIGHT_SENTENCES.items():
        expected = cat[ns.sentence_msg_key(key)].replace(
            "{fan, select, hvac {HVAC fan} other {whole-house fan}}", "{fan}"
        )
        assert expected == tmpl.template, key


def test_source_note_constant_equals_the_catalog_entry() -> None:
    assert _en("night")["night.source_note"] == ns.SOURCE_NOTE


# ---------------------------------------------------------------------------------------------- learning
def test_low_window_compliance_english_is_unchanged_and_the_wire_round_trips(tmp_path: Path) -> None:
    e = _engine(tmp_path)
    _seed_window_compliance_records(e, 14)
    (text,) = e.generate_suggestions()
    # Issue #1652: the text no longer offers to stop window tips (no setting does that); it is advice now.
    assert text == (
        "Over the past 14 days where opening windows was recommended, they were opened only 0% of the time. "
        "Opening them is optional: with the windows closed, Climate Advisor keeps the house comfortable with the "
        "HVAC, which uses slightly more energy."
    )
    (msg,) = e.get_last_suggestion_msgs()
    assert e.get_last_suggestion_keys() == ["low_window_compliance"]
    assert i18n.Msg.from_wire(msg.to_wire()) == msg


def test_low_window_compliance_in_another_language_substitutes_every_param(tmp_path: Path, fr_catalog) -> None:
    fr_catalog(
        {
            "learning.suggestion.low_window_compliance": (
                "Sur {days, plural, one {# jour} other {# jours}} : fenetres ouvertes {compliance, number, ::percent}."
            )
        }
    )
    e = _engine(tmp_path)
    _seed_window_compliance_records(e, 14)
    (text,) = e.generate_suggestions("fr")
    assert text == "Sur 14 jours : fenetres ouvertes 0\xa0%."
    assert "Over the past" not in text and "{" not in text


def test_frequent_overrides_detail_with_and_without_peak_hour(tmp_path: Path) -> None:
    e = _engine(tmp_path)
    details = [{"direction": "up", "magnitude": 2.0, "time": "07:15"} for _ in range(11)]
    e._state.records.extend(_rec(date=f"2026-03-{i + 1:02d}", manual_overrides=11 if i == 0 else 0) for i in range(14))
    e._state.records[0]["override_details"] = details
    (text,) = e.generate_suggestions()
    assert text == (
        "You've manually adjusted the thermostat 11 times in the past two weeks "
        "(mostly up by about 2°F, often around 7:00). If the comfort temperatures don't suit you, change them in "
        "Settings and Climate Advisor will hold the new ones."
    )
    # celsius engine: the delta is a scale-only TempDelta in the engine's unit
    e._temp_unit = "celsius"
    (text_c,) = e.generate_suggestions()
    assert "mostly up by about 1°C, often around 7:00" in text_c
    # no granular data
    e._state.records[0]["override_details"] = []
    (bare,) = e.generate_suggestions()
    assert bare == (
        "You've manually adjusted the thermostat 11 times in the past two weeks. This may mean the comfort "
        "temperatures don't match your preferences; you can change them in Settings."
    )


def test_pause_and_runtime_and_static_suggestions_english(tmp_path: Path) -> None:
    e = _engine(tmp_path)
    e._state.records.extend(
        _rec(
            date=f"2026-03-{i + 1:02d}",
            hvac_runtime_minutes=130.0,
            door_window_pause_events=3,
            door_pause_by_sensor={"front_door": 2, "back_door": 1},
            comfort_violations_minutes=40.0,
            occupancy_away_minutes=35.0,
        )
        for i in range(14)
    )
    got = dict(zip(e.generate_suggestions() and e.get_last_suggestion_keys(), e.generate_suggestions(), strict=True))
    assert got["high_runtime_mild_days"].startswith(
        "On mild and warm days, the HVAC has been running an average of 130 minutes"
    )
    # Issue #1652: advice now; there are no mild-day setpoints for Accept to adjust
    assert got["high_runtime_mild_days"].endswith(
        "Adding door/window sensors or widening the comfort range in Settings can help."
    )
    # Issue #1652 (owner decision): frequent door pauses and comfort misses no longer produce a suggestion.
    assert "frequent_door_pauses" not in got and "comfort_violations" not in got
    assert got["short_departures"].startswith("You frequently leave for 30–45 minute periods")


def test_suggestion_text_is_never_persisted(tmp_path: Path) -> None:
    e = _engine(tmp_path)
    _seed_window_compliance_records(e, 14)
    e.generate_suggestions()
    assert "Over the past" not in json.dumps(e._state.__dict__, default=str)


# ---------------------------------------------------------------------------------------------- night strategy
def test_cold_night_headline_and_labels_english() -> None:
    st = ns.plan_night(cold_night())
    assert ns.headline(st) == "Keep the windows closed tonight; CA keeps the heat for the bedroom at 66°F."
    labels = {o.key: o.label for o in st.options}
    assert labels[ns.OPT_CLOSED] == "Keep the windows closed; CA heats or cools the bedroom as needed"
    assert labels[ns.OPT_WINDOWS_2H] == "Open the bedroom windows for 2 h at bedtime, then close them"
    assert labels[ns.OPT_FAN_2H] == "Whole-house fan with the windows open for 2 h, then close up"
    assert labels[ns.OPT_WINDOWS_ALL] == "Leave the bedroom windows open all night"
    notes = {o.key: o.note for o in st.options}
    assert notes[ns.OPT_WINDOWS_ALL] == "windows stay open until you get up"
    assert notes[ns.OPT_FAN_UNTIL_TARGET] == "would not run tonight: the bedroom does not need it"


def test_warm_night_headline_names_the_close_time_in_12h_clock() -> None:
    st = dataclasses.replace(ns.plan_night(warm_night()), recommended=ns.OPT_WINDOWS_2H)
    text = ns.headline(st)
    assert text is not None and re.search(r"^Open the bedroom windows until \d{1,2}:\d{2} [AP]M, then close up; ", text)
    assert ns.headline_msg(st).key == "night.headline"


def test_headline_close_time_formats_per_language(fr_catalog) -> None:
    close = datetime(2026, 11, 7, 1, 0)
    msg = i18n.Msg("night.headline_until_approx", what="windows", t=i18n.Time(close))
    assert i18n.render_en(msg) == "Open the bedroom windows until about 1:00 AM, then close up"
    fr_catalog({"night.headline_until_approx": "Ouvrir les fenetres jusqu'a environ {t}, puis fermer"})
    fr = i18n.render(msg, "fr")
    assert fr.startswith("Ouvrir les fenetres jusqu'a environ 1:00") and "AM" not in fr


def test_tradeoffs_render_in_english_and_carry_wire_siblings() -> None:
    st = ns.plan_night(cold_night())
    rows = ns.tradeoffs(st)
    assert rows
    for r in rows:
        assert i18n.render_en(i18n.Msg.from_wire(r["text_msg"])) == r["text"]
        assert i18n.render_en(i18n.Msg.from_wire(r["label_msg"])) == r["label"]
    wa = next(r for r in rows if r["key"] == ns.OPT_WINDOWS_ALL)
    assert wa["text"].startswith(
        "Leave the bedroom windows open all night: 27.0 more F-hours outside your sleep band, "
    )
    assert ", and 1 more thing(s) for you to do" in wa["text"]
    celsius = ns.tradeoffs(st, "celsius")
    assert "15.0 more C-hours" in next(r for r in celsius if r["key"] == ns.OPT_WINDOWS_ALL)["text"]


def test_tradeoff_in_another_language_uses_the_locale_list(fr_catalog) -> None:
    st = ns.plan_night(cold_night())
    fr_catalog(
        {
            "night.tradeoff": "{label} : {parts}",
            "night.tradeoff_disc_more": "{n, number, ::.0} {deg}-heures de plus hors de votre plage de sommeil",
            "night.tradeoff_hvac_more": "{n, plural, one {# minute} other {# minutes}} de plus",
            "night.tradeoff_hvac_fewer": "{n} min de moins",
            "night.tradeoff_actions_more": "{n, plural, one {# chose} other {# choses}} de plus",
            "night.option_windows_all": "Fenetres ouvertes toute la nuit",
        }
    )
    row = next(r for r in ns.tradeoffs(st, lang="fr") if r["key"] == ns.OPT_WINDOWS_ALL)
    assert row["text"].startswith("Fenetres ouvertes toute la nuit : 27,0 F-heures de plus")
    assert " et " in row["text"] and "more" not in row["text"]


def test_api_payload_prose_and_msg_siblings(fr_catalog) -> None:
    st = ns.plan_night(cold_night())
    body = ns.api_payload(st, unit="fahrenheit", convert=lambda v, u: v)
    assert body["source_note"] == ns.SOURCE_NOTE
    assert i18n.Msg.from_wire(body["source_note_msg"]) == ns.SOURCE_NOTE_MSG
    assert body["headline"].startswith("Keep the windows closed tonight")
    assert i18n.render_en(i18n.Msg.from_wire(body["headline_msg"])) == body["headline"]
    for o in body["options"]:
        assert i18n.render_en(i18n.Msg.from_wire(o["label_msg"])) == o["label"]
    fr_catalog({"night.source_note": "De v4.", "night.option_closed": "Fenetres fermees"})
    fr = ns.api_payload(st, unit="fahrenheit", convert=lambda v, u: v, lang="fr")
    assert fr["source_note"] == "De v4."
    assert next(o for o in fr["options"] if o["key"] == ns.OPT_CLOSED)["label"] == "Fenetres fermees"
    assert fr["headline_msg"] == body["headline_msg"]  # the wire is language-free


@pytest.mark.parametrize(
    ("key", "facts", "expected"),
    [
        (
            "night_strategy.windows_open",
            {"t": datetime(2026, 11, 6, 21, 5, tzinfo=UTC)},
            "9:05 PM: open the bedroom windows; the outdoor air is cooler than the bedroom.",
        ),
        (
            "night_strategy.windows_close_predicted",
            {"t": datetime(2026, 11, 7, 0, 0, tzinfo=UTC), "target": 67.0, "floor": 66.0},
            "12:00 AM: close the bedroom windows (about when the bedroom reaches 67°F); CA keeps it at 66°F+.",
        ),
        (
            "night_strategy.fan_on",
            {"t": datetime(2026, 11, 6, 22, 30, tzinfo=UTC), "fan": ns.FAN_WORD_HVAC},
            "10:30 PM: turn the HVAC fan on with the windows open to cool the bedroom.",
        ),
        (
            "night_strategy.fan_off",
            {"t": datetime(2026, 11, 7, 1, 0, tzinfo=UTC), "fan": ns.FAN_WORD_WHOLE_HOUSE},
            "1:00 AM: turn the whole-house fan off; CA takes over holding the bedroom.",
        ),
    ],
)
def test_sentences_english_is_unchanged(key: str, facts: dict, expected: str) -> None:
    assert ns._sentence(key, "fahrenheit", **facts) == expected
    msg = ns.sentence_msg(key, "fahrenheit", **facts)
    assert i18n.Msg.from_wire(msg.to_wire()) == msg
    assert i18n.render_en(msg) == expected


def test_sentence_celsius_and_missing_fact() -> None:
    text = ns._sentence("night_strategy.windows_close", "celsius", t=datetime(2026, 11, 7, 1, 0), floor=66.0)
    assert text == "1:00 AM: close the bedroom windows; CA keeps the bedroom at 19°C or above."
    with pytest.raises(KeyError):
        ns._sentence("night_strategy.windows_close", "fahrenheit", t=datetime(2026, 11, 7, 1, 0))


def test_sentence_in_another_language_formats_time_and_temp_per_locale(fr_catalog) -> None:
    fr_catalog({"night.sentence_windows_close": "{t} : fermez les fenetres ; CA maintient {floor} ou plus."})
    msg = ns.sentence_msg("night_strategy.windows_close", "celsius", t=datetime(2026, 11, 7, 13, 30), floor=66.5)
    out = i18n.render(msg, "fr")
    assert out.startswith("13:30 : fermez") and "19" in out and "PM" not in out and "{" not in out
