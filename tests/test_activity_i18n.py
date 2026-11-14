"""The Activity record as messages (Issue #1341, lane L7).

Pins: every ``_m("key")`` call in the renderer modules names a catalog key whose ICU arguments match the keyword
arguments passed; the English the renderers produce is unchanged (the 87 asserts in test_activity_renderers.py);
a Msg cell survives the wire round trip and renders in another language with its typed parameters; an event
emitted with a Msg keeps its English ``reason`` and gains ``reason_key``/``reason_params``.
"""

from __future__ import annotations

import ast
import datetime
import json
import sys
from pathlib import Path

import pytest

if "homeassistant" not in sys.modules:
    from tools.sim_harness.ha_stubs import install_ha_stubs

    install_ha_stubs()

import custom_components.climate_advisor.ai_skills_context as ctx  # noqa: E402
from custom_components.climate_advisor import i18n  # noqa: E402
from custom_components.climate_advisor.record_text import record_en  # noqa: E402

PKG = Path(ctx.__file__).parent
CATALOG = json.loads((PKG / "frontend" / "i18n" / "activity" / "en.json").read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def _catalogs():
    i18n.reset_catalogs()
    i18n.set_strict(True)
    i18n.load_catalogs()
    yield
    i18n.set_strict(None)
    i18n.reset_catalogs()


def _icu_arguments(nodes) -> set[str]:
    """Every argument name a parsed ICU message takes (including those inside select/plural options)."""
    names: set[str] = set()
    for node in nodes:
        if node[0] in ("arg", "number"):
            names.add(node[1])
        elif node[0] in ("select", "plural"):
            names.add(node[1])
            for body in node[2].values():
                names |= _icu_arguments(body)
    return names


def _m_calls(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_m"
            and node.args
            and isinstance(node.args[0], ast.Constant)
        ):
            yield node


def test_every_renderer_key_exists_with_matching_arguments():
    problems = []
    for node in _m_calls(PKG / "ai_skills_context.py"):
        key = "activity." + node.args[0].value
        if key not in CATALOG:
            problems.append(f"missing {key}")
            continue
        if any(kw.arg is None for kw in node.keywords):
            continue
        passed = {kw.arg for kw in node.keywords}
        taken = _icu_arguments(i18n.parse_message(CATALOG[key]))
        if passed != taken:
            problems.append(f"{key}: passed {sorted(passed)} vs catalog {sorted(taken)}")
    assert not problems, problems


def test_sensor_reason_label_tables_match_the_catalog():
    for code, text in ctx.SENSOR_REASON_LABELS.items():
        assert CATALOG[f"activity.sensor_reason_{code}"] == text


def test_every_registered_renderer_has_a_message_form():
    assert all(hasattr(fn, "msg") for fn in ctx.EVENT_RENDERERS.values())


def test_msg_cells_round_trip_the_wire_and_match_the_english_text():
    payload = {
        "indoor_temp": 77.0,
        "off_threshold": 70.0,
        "fan_device": "whf",
        "reason": "x",
    }
    ev, settings = ctx.EVENT_RENDERERS["nat_vent_fan_off"].msg(payload, "fahrenheit")
    assert isinstance(ev, i18n.Msg)
    again = i18n.Msg.from_wire(json.loads(json.dumps(ev.to_wire())))
    assert record_en(again) == record_en(ev) == "Nat-vent fan off -- indoor 77°F <= 70°F"
    assert record_en(settings) == "WHF: on->off"
    assert ctx.EVENT_RENDERERS["nat_vent_fan_off"](payload, "fahrenheit") == (
        "Nat-vent fan off -- indoor 77°F <= 70°F",
        "WHF: on->off",
    )


def test_a_message_cell_renders_in_another_language_with_its_typed_params(monkeypatch):
    # The shipped non-English catalogs arrive in the translation pass; a stand-in French pins that the typed
    # temperature parameter and the nested fan-transition message render through the target language.
    fr = i18n._CATALOGS["fr"]
    monkeypatch.setitem(
        fr, "activity.nat_vent_fan_off_indoor", "Ventilation arrêtée -- intérieur {indoor} <= {threshold}"
    )
    monkeypatch.setitem(fr, "activity.fan_whf_off", "VMC : marche->arrêt")
    ev, settings = ctx.EVENT_RENDERERS["nat_vent_fan_off"].msg(
        {"indoor_temp": 77.0, "off_threshold": 70.0, "fan_device": "whf"}, "celsius"
    )
    assert i18n.render(ev, "fr") == "Ventilation arrêtée -- intérieur 25°C <= 21°C"
    assert i18n.render(settings, "fr") == "VMC : marche->arrêt"
    assert i18n.render(ev, "en") == "Nat-vent fan off -- indoor 25°C <= 21°C"


def test_a_stored_message_field_is_re_rendered_from_its_key():
    reason = i18n.Msg("activity.held_whf_session")
    p = {"reason": record_en(reason), "reason_key": reason.key, "reason_params": {}, "mode": "heat"}
    label, _settings = ctx.EVENT_RENDERERS["comfort_band_applied"].msg(p, "fahrenheit")
    assert isinstance(label, i18n.Msg)
    assert record_en(label) == "Comfort band applied (heat) -- whole-house fan running"


def test_timeline_wire_rows_carry_event_and_settings_messages():
    now = datetime.datetime(2026, 6, 17, 14, 0, tzinfo=datetime.UTC)
    log = [
        {
            "time": (now - datetime.timedelta(hours=1)).isoformat(),
            "type": "fan_cancel",
            "fan_before": "on",
            "fan_after": "off",
        },
        {"time": (now - datetime.timedelta(minutes=30)).isoformat(), "type": "some_unregistered_type", "reason": "why"},
    ]
    wire = ctx.build_event_timeline_wire(log, {"temp_unit": "fahrenheit"}, 24, now, newest_first=True)
    rows = wire["rows"]
    assert [r["time"] for r in rows] == sorted((r["time"] for r in rows), reverse=True)
    cancel = next(r for r in rows if r["event"].startswith("Fan cancel"))
    assert i18n.render(i18n.Msg.from_wire(cancel["event_msg"]), "en") == cancel["event"]
    assert cancel["settings"] == "fan: on->off"
    assert i18n.render(i18n.Msg.from_wire(cancel["settings_msg"]), "en") == "fan: on->off"
    assert wire["limited_msg"] is None
    # The English table builder is unchanged.
    table = ctx.build_event_timeline_table(log, {"temp_unit": "fahrenheit"}, 24, now)
    assert "Fan cancel (user turned off)" in table


def test_collapsed_run_row_is_a_message():
    now = datetime.datetime(2026, 6, 17, 14, 0, tzinfo=datetime.UTC)
    log = [
        {"time": (now - datetime.timedelta(minutes=20 - i)).isoformat(), "type": "thermal_learning_no_observations"}
        for i in range(3)
    ]
    wire = ctx.build_event_timeline_wire(log, {}, 24, now)
    (row,) = wire["rows"]
    assert row["event"].startswith("Thermal learning no observations x3 (")
    assert i18n.render(i18n.Msg.from_wire(row["event_msg"]), "en") == row["event"]


def test_coordinator_event_keeps_english_reason_and_adds_key_fields():
    from custom_components.climate_advisor.coordinator import ClimateAdvisorCoordinator

    msg = i18n.Msg("activity.held_whf_session")
    out = ClimateAdvisorCoordinator._split_msg_fields(
        {"reason": msg, "fan_mode_change": i18n.Msg("activity.fan_whf_on"), "n": 1}
    )
    assert out["reason"] == "whole-house fan running"
    assert out["reason_key"] == "activity.held_whf_session"
    assert out["reason_params"] == {}
    assert out["fan_mode_change"] == "WHF: off->on"
    assert out["fan_mode_change_key"] == "activity.fan_whf_on"
    legacy = {"reason": "plain text"}
    assert ClimateAdvisorCoordinator._split_msg_fields(legacy) is legacy


def test_msg_text_is_the_english_string_and_carries_the_message():
    from custom_components.climate_advisor.record_text import MsgText, msg_of, msg_text

    msg = i18n.Msg(
        "activity.nat_vent_exit_outdoor_gt_indoor",
        outdoor=i18n.Temp(80.0, "fahrenheit"),
        indoor=i18n.Temp(70.0, "fahrenheit"),
    )
    text = msg_text(msg)
    assert isinstance(text, MsgText)
    assert text == "Nat-vent exit -- outdoor 80°F > indoor 70°F"
    assert text.startswith("Nat-vent exit") and "indoor 70°F" in text
    assert msg_of(text) is msg and msg_of(msg) is msg and msg_of("plain") is None
    assert msg_text("plain") == "plain"
    from custom_components.climate_advisor.coordinator import ClimateAdvisorCoordinator

    out = ClimateAdvisorCoordinator._split_msg_fields({"reason": text})
    assert out["reason"] == str(text) and type(out["reason"]) is str
    assert out["reason_key"] == msg.key
    assert i18n.Msg.from_wire({"key": out["reason_key"], "params": out["reason_params"]}) == msg
