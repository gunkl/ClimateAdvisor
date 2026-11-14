"""The operation timeline and its surfaces as catalog messages (Issue #1341, lane L5, area ``timeline``).

Pins: every table sentence renders from the catalog with the exact English its template always had (both units, a
same-day and an other-day time); the plan-list row wording from the catalog equals the old regex rewrite; a step keeps
its message through the contract dict; a language's catalog drives the typed parameters (times, temperatures, the
weekday); the cards and Next Automation labels are messages and ``advises_opening`` reads the KEY; the v4 words are
whole-sentence messages whose English is unchanged.
"""

from __future__ import annotations

import ast
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from custom_components.climate_advisor import i18n, v4_shadow
from custom_components.climate_advisor import operation_timeline as ot
from custom_components.climate_advisor import timeline_surfaces as ts
from custom_components.climate_advisor.night_strategy import NIGHT_SENTENCES
from tests.timeline_fixtures import MODEL, TZ, make_snapshot

PKG = Path(ot.__file__).parent
CATALOG = json.loads((PKG / "frontend" / "i18n" / "timeline" / "en.json").read_text(encoding="utf-8"))
NOW = datetime(2026, 11, 7, 20, 30, tzinfo=TZ)  # a Saturday
NEXT_DAY = datetime(2026, 11, 8, 6, 15, tzinfo=TZ)

_TEXT_FACTS = {"sensor": "bedroom sensor", "fan": "whole-house fan", "old": "heat", "new": "cool", "base": ""}


@pytest.fixture(autouse=True)
def _catalogs():
    i18n.reset_catalogs()
    i18n.set_strict(True)
    i18n.load_catalogs()
    yield
    i18n.set_strict(None)
    i18n.reset_catalogs()


def _facts(spec: dict[str, str], at: datetime) -> dict:
    out: dict = {}
    for name, kind in spec.items():
        if kind == "time":
            out[name] = at if name == "t" else NEXT_DAY
        elif kind == "temp":
            out[name] = 68.0
        elif kind == "delta":
            out[name] = 6.0
        elif kind == "int":
            out[name] = 2
        elif kind == "minutes":
            out[name] = 32.0
        elif name == "occupancy":
            out[name] = "away"
        else:
            out[name] = _TEXT_FACTS.get(name, "x")
    return out


TABLE_KEYS = sorted(k for k in ot.SENTENCES if k not in NIGHT_SENTENCES)


@pytest.mark.parametrize("unit", ["fahrenheit", "celsius"])
@pytest.mark.parametrize("at", [NOW, NEXT_DAY], ids=["same_day", "next_day"])
@pytest.mark.parametrize("key", TABLE_KEYS)
def test_the_catalog_sentence_is_the_english_the_template_always_rendered(key: str, unit: str, at: datetime) -> None:
    facts = _facts(dict(ot.SENTENCES[key].facts), at)
    assert ot.say(key, unit, ref=NOW, **facts) == ot._say_registered(key, unit, NOW, facts)


@pytest.mark.parametrize("key", sorted(ot.STEP_SHORT_KEYS))
def test_the_row_wording_from_the_catalog_is_the_old_rewrite(key: str) -> None:
    actor = "you" if key.startswith("windows_") else "ca"
    msg = ot.sentence_msg(key, "fahrenheit", ref=NOW, **_facts(dict(ot.SENTENCES[key].facts), NOW))
    assert i18n.render_en(ot.short_msg(msg)) == ts.plan_list_text(i18n.render_en(msg), actor)


def test_a_held_step_shortens_the_step_it_wraps() -> None:
    base = ot.sentence_msg("sleep_band.default", "fahrenheit", ref=NOW, t=NOW, floor=66.0, ceiling=72.0)
    held = ot.sentence_msg("held.suffix", "fahrenheit", ref=NOW, base=base, until=NEXT_DAY)
    assert i18n.render_en(held) == ot.say("held.suffix", ref=NOW, base=i18n.render_en(base), until=NEXT_DAY)
    assert i18n.render_en(ot.short_msg(held)) == ts.plan_list_text(i18n.render_en(held), "ca")
    assert i18n.render_en(ot.short_msg(held)).startswith("Bedtime: switches to the sleep band")


def _tl():
    snap = make_snapshot("mild", now=datetime(2026, 11, 4, 8, 0, tzinfo=TZ), indoor=70.0, sleep_sensor="bedroom sensor")
    return ot.build_timeline(snap)


def test_every_step_carries_its_message_and_the_text_is_its_english() -> None:
    tl = _tl()
    assert tl.steps
    for s in tl.steps:
        assert s.msg is not None
        assert s.text == i18n.render_en(s.msg)
    assert [i18n.render_en(m) for m in tl.summary_msgs] == list(tl.summary)


def test_the_message_survives_the_contract_dict() -> None:
    tl = _tl()
    d = json.loads(json.dumps(ot.to_dict(tl)))
    assert all("text_msg" in s for s in d["steps"])
    back = ot.from_dict(d)
    assert back is not None
    assert [s.msg for s in back.steps] == [s.msg for s in tl.steps]
    assert [i18n.Msg.from_wire(w) for w in ot.detail_dict(tl)["summary_msg"]] == list(tl.summary_msgs)


def _fake_language(monkeypatch, lang: str, entries: dict[str, str]) -> None:
    monkeypatch.setitem(i18n._CATALOGS, lang, {**i18n._CATALOGS.get(lang, {}), **entries})


def test_a_language_catalog_drives_the_typed_parameters(monkeypatch) -> None:
    _fake_language(
        monkeypatch,
        "de",
        {
            "timeline.step.wake_band.default": "<{t}> Komfortband {floor} bis {ceiling}",
            "timeline.when.other_day": "{weekday} um {t}",
            "timeline.weekday.sun": "So",
        },
    )
    msg = ot.sentence_msg("wake_band.default", "celsius", ref=NOW, t=NEXT_DAY, floor=68.5, ceiling=74.0)
    out = i18n.render(msg, "de")
    assert out.startswith("<So um ") and "Komfortband 20" in out and "23" in out  # the German catalog's words
    assert "°C" in out and "CA switches" not in out
    assert i18n.render_en(msg) == "Sun 6:15 AM: CA switches to the daytime comfort band, 20°C to 23°C."
    # a language that has no entry falls back to English per key, never to the key
    fr = i18n.render(msg, "fr")
    assert "CA switches to the daytime comfort band, 20°C to 23°C." in fr and "timeline." not in fr


def test_the_rest_payload_renders_in_the_language_and_keeps_the_wire(monkeypatch) -> None:
    tl = _tl()
    _fake_language(monkeypatch, "de", {"timeline.step_short.briefing_send.default": "Sendet das Briefing."})
    body = ts.timeline_api_payload(ot.to_dict(tl), ot.detail_dict(tl), "fahrenheit", lambda v, u: v, "de")
    sends = [s for s in body["steps"] if s["kind"] == "briefing_send"]
    assert sends and all(s["text_short"] == "Sendet das Briefing." for s in sends)
    for s in body["steps"]:
        assert i18n.Msg.from_wire(s["text_msg"]).key.startswith("timeline.step.")
        assert i18n.Msg.from_wire(s["text_short_msg"]).key.startswith(("timeline.step_short.", "timeline.step."))
    en = ts.timeline_api_payload(ot.to_dict(tl), ot.detail_dict(tl), "fahrenheit", lambda v, u: v)
    assert [s["text"] for s in en["steps"]] == [s.text for s in tl.steps]
    # English byte-identical to the old regex rewrite
    assert [s["text_short"] for s in en["steps"]] == [ts.plan_list_text(s.text, s.actor.value) for s in tl.steps]


def test_the_strips_carry_message_siblings() -> None:
    snap = make_snapshot("warm", now=datetime(2026, 11, 4, 8, 0, tzinfo=TZ), indoor=70.0)
    tl = ot.build_timeline(snap)
    spans = ts.strip_spans(tl, snap.now)
    assert spans
    for d in spans:
        wire = d.get("text_msg")
        assert wire is not None, d
        assert i18n.render_en(i18n.Msg.from_wire(wire)) == d["text"]


@pytest.mark.parametrize("mode", ["heat", "cool"])
@pytest.mark.parametrize("placed", [True, False])
def test_the_run_sentences_keep_their_english(mode: str, placed: bool) -> None:
    start, end = NOW, NOW + timedelta(hours=2)
    text = i18n.render_en(ts._run_msg(mode, 22.0, start, end, placed=placed))
    verb, edge = ("Heating", "floor") if mode == "heat" else ("Cooling", "ceiling")
    if placed:
        assert text == f"{verb} expected for about 20 min between 8:30 PM and 10:30 PM, holding the band's {edge}."
    else:
        assert text == (
            f"{verb} expected for about 20 min, around 8:30 PM (the plan's window runs to 10:30 PM), "
            f"holding the band's {edge}."
        )


# ---------------------------------------------------------------- the cards
def _card_live(snap, windows_open: bool, direction_ok: bool) -> ts.CardLive:
    return ts.CardLive(
        unit=snap.unit,
        indoor=snap.indoor,
        outdoor=snap.outdoor,
        windows_open=windows_open,
        direction_ok=direction_ok,
        heat_side_veto=False,
        armed_heat=68.0,
        armed_cool=74.0,
    )


def test_the_window_card_is_a_message_and_advises_opening_reads_its_key() -> None:
    now = datetime(2026, 11, 4, 8, 0, tzinfo=TZ)
    snap = make_snapshot("mild", now=now, indoor=70.0, model=dict(MODEL, observation_count_vent_window=12))
    tl = ot.build_timeline(snap)
    got = ts.window_card_text(tl, now, _card_live(snap, False, True))
    assert got is not None
    text, _step = got
    assert isinstance(text, str) and text.startswith("Open windows at ")
    assert text.msg.key == "timeline.card.open_at" and ts.advises_opening(text)
    assert text.msg.to_wire()["params"]["t"]["$t"] == "time"
    # a bare string carries no key: the English words never decide
    assert not ts.advises_opening("Open windows at 8:00 AM")
    assert not ts.advises_opening(i18n.Msg("timeline.card.close_by", t=i18n.Time(now)))
    temps = {"outdoor": i18n.Temp(58, "fahrenheit"), "indoor": i18n.Temp(70, "fahrenheit")}
    assert ts.advises_opening(i18n.Msg("timeline.card.open_cooler", **temps))


def test_every_card_message_renders_its_english() -> None:
    now = datetime(2026, 11, 4, 8, 0, tzinfo=TZ)
    temps = {"outdoor": i18n.Temp(58, "fahrenheit"), "indoor": i18n.Temp(70, "fahrenheit")}
    assert i18n.render_en(i18n.Msg("timeline.card.open_cooler", **temps)) == (
        "Open windows — outdoor's cooler now (58°F vs 70°F)."
    )
    assert i18n.render_en(i18n.Msg("timeline.card.open_briefly", **temps)) == (
        "Open windows briefly — outdoor's warmer (58°F vs 70°F)."
    )
    assert i18n.render_en(i18n.Msg("timeline.card.close_hold_heat", **temps)) == (
        "Close windows to hold heat — outdoor's cooler (58°F vs 70°F)."
    )
    assert i18n.render_en(i18n.Msg("timeline.card.close_not_helping", **temps)) == (
        "Close windows — outdoor's not helping now (58°F vs 70°F)."
    )
    assert i18n.render_en(i18n.Msg("timeline.card.open_unknown")) == "Open windows — outdoor air may be cooler now."
    assert i18n.render_en(i18n.Msg("timeline.card.close_by", t=i18n.Time(now))) == "Close windows by 8:00 AM"


def test_next_automation_labels_are_messages() -> None:
    snap = make_snapshot("warm", now=datetime(2026, 11, 4, 8, 0, tzinfo=TZ), indoor=70.0)
    tl = ot.build_timeline(snap)
    labels = [(s, ts.next_automation_label(s, snap.unit)) for s in tl.steps if s.actor.value == "ca"]
    labelled = [(s, lab) for s, lab in labels if lab is not None]
    assert labelled
    for _s, lab in labelled:
        assert lab.msg is not None and lab.msg.key in set(ts.NEXT_AUTOMATION_LABEL_KEYS.values())
        assert lab == i18n.render_en(lab.msg)
    action, at, _step = ts.next_automation(tl, tl.generated_at + timedelta(days=5), snap.unit)
    assert (action, at) == ("No more actions today", "")
    assert action.msg.key == ts.NEXT_AUTOMATION_NONE_KEY
    assert ts.tou_bank_end_label("back to the Away band 60–80°F") == (
        "High-cost period starts — back to the Away band 60–80°F"
    )
    assert ts.tou_bank_end_label("x").msg.to_wire()["params"] == {"after": "x"}


# ---------------------------------------------------------------- the v4 words
_SUMMARY = {
    "has_model": True,
    "confidence": "low",
    "span_days": 30,
    "sample_days": 6.4,
    "confidence_reason": "too few accepted thermostat starts in a used mode",
    "modes": {
        "heat": {"seen": True, "starts": 61, "accepted": 45, "grade": None, "reason": None, "carried_grade": False},
        "cool": {"seen": True, "starts": 2, "accepted": 0, "grade": None, "reason": None, "carried_grade": False},
    },
}


def test_the_grade_words_are_whole_messages_with_the_english_unchanged() -> None:
    why, by_mode = v4_shadow.describe_grade_msgs(_SUMMARY)
    assert isinstance(why, i18n.Msg) and isinstance(by_mode, i18n.Msg)
    assert i18n.render_en(why) == "too few usable cooling starts (ok needs 5 in each mode it has seen)"
    assert i18n.render_en(by_mode) == (
        "heating 45 usable of 61 starts (enough) · cooling 0 usable of 2 starts (needs 5) in the last 30 days"
    )
    assert i18n.Msg.from_wire(json.loads(json.dumps(why.to_wire()))) == why  # the wire round trip
    assert v4_shadow.describe_grade_msgs({"has_model": False}) == (None, None)


def test_a_language_catalog_renders_the_grade_words(monkeypatch) -> None:
    _fake_language(
        monkeypatch,
        "de",
        {
            "timeline.v4.why.too_few_starts": "zu wenige {modes}-Starts (nötig: {need})",
            "timeline.v4.word.cooling": "Kühlung",
        },
    )
    why, _by = v4_shadow.describe_grade_msgs(_SUMMARY)
    assert i18n.render(why, "de") == "zu wenige Kühlung-Starts (nötig: 5)"


def test_unusable_refit_sentences() -> None:
    rec = {"count": 3, "fit_reason": "fit diverged", "kept_fitted_at": "2026-11-10T04:30:00+00:00"}
    assert v4_shadow.describe_unusable_fit(rec) == (
        "last 3 refits unusable (fit diverged); keeping the model from 2026-11-10"
    )
    assert v4_shadow.describe_unusable_fit({"count": 1}) == (
        "last refit unusable (confidence none); keeping the model from an earlier fit"
    )
    assert v4_shadow.describe_unusable_fit(None) == "" and v4_shadow.describe_unusable_fit_msg(None) is None
    assert v4_shadow.unusable_fit_notice({"count": 1}) == ""
    assert v4_shadow.unusable_fit_notice(rec) == (
        "v4 could not refit your home the last 3 times (fit diverged); pre-heat, pre-cool and the other decisions "
        "still use the model from 2026-11-10"
    )
    # an unknown stored reason is shown as stored
    unknown = {"count": 1, "fit_reason": "fit failed: ValueError"}
    assert "(fit failed: ValueError)" in v4_shadow.describe_unusable_fit(unknown)


_CHOICE = {
    "model": "per_room",
    "primary": "sensor.a",
    "sleep": "sensor.b",
    "primary_days": 5.0,
    "sleep_days": 4.5,
    "min_days": 3.0,
    "lead": "4h",
    "reason": "per_room_no_worse",
    "scores": {"mae": {"stitched": {"1h": 0.5, "4h": 0.8, "12h": 0.9}, "per_room": {"1h": 0.4, "4h": 0.7, "12h": 0.8}}},
}


def test_node_choice_words_in_the_readers_unit_and_the_legacy_text_unchanged() -> None:
    names = {"sensor.a": "Hallway", "sensor.b": "Bedroom"}
    head, reason = v4_shadow.describe_node_choice_msgs(_CHOICE, unit="fahrenheit", names=names, changed=True)
    assert i18n.render_en(head) == "v4 now forecasts each room separately (thermostat by day, bedroom at night)"
    text = i18n.render_en(reason)
    assert text.startswith("both rooms have 3+ days of readings (Hallway 5.0 days, Bedroom 4.5 days) and the per-room")
    assert "0.70°F vs 0.80°F at 4 h (1 h 0.40°F vs 0.50°F, 12 h 0.80°F vs 0.90°F)" in text
    legacy_head, legacy_reason = v4_shadow.describe_node_choice(_CHOICE, names=names)
    assert legacy_head == "v4 forecasts each room separately (thermostat by day, bedroom at night)"
    assert legacy_reason == text
    wire = json.loads(json.dumps(reason.to_wire()))
    assert i18n.render_en(i18n.Msg.from_wire(wire)) == text  # typed TempDelta params survive the wire
    assert v4_shadow.describe_node_choice_msgs(None) == (None, None)


# ---------------------------------------------------------------- the catalog itself
def _literal_keys(path: Path) -> set[str]:
    keys: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.startswith("timeline."):
            keys.add(node.value)
    return keys


def test_every_literal_timeline_key_in_the_lane_modules_exists_in_the_catalog() -> None:
    missing: list[str] = []
    for name in ("timeline_surfaces.py", "operation_timeline.py", "v4_shadow.py"):
        for key in _literal_keys(PKG / name):
            if any(k.startswith(key) for k in CATALOG if k != key):
                continue  # a prefix the code completes (a sentence key, a weekday, a mode, a run's heat/cool)
            if key not in CATALOG:
                missing.append(f"{name}: {key}")
    assert not missing


def test_every_generated_key_family_is_complete() -> None:
    for key in TABLE_KEYS:
        assert f"timeline.step.{key}" in CATALOG, key
    for key in ot.STEP_SHORT_KEYS:
        assert f"timeline.step_short.{key}" in CATALOG, key
    for day in ot._WEEKDAYS:
        assert f"timeline.weekday.{day}" in CATALOG
    for table in ot._TEXT_CODES.values():
        for catalog_key in table.values():
            assert catalog_key in CATALOG, catalog_key
    for catalog_key in ts.NEXT_AUTOMATION_LABEL_KEYS.values():
        assert catalog_key in CATALOG, catalog_key
    for slug in v4_shadow._FIT_REASON_KEYS.values():
        assert f"timeline.v4.reason.{slug}" in CATALOG, slug
    for word in ("heat", "cool"):
        for placed in ("placed", "around"):
            assert f"timeline.strip.run_{word}_{placed}" in CATALOG
