"""Cross-lane wiring, PR A (Issue #1341): the L3-owned automation blocks, the API and the suggestions sensor.

Each converted setter reason / ``_record_action`` literal renders its exact old English (the record) and, through a
temporary ``fr`` catalog, another language with every parameter substituted; the API adds ``*_msg`` siblings and
never removes a field.
"""

from __future__ import annotations

import asyncio
import datetime
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

if "homeassistant" not in sys.modules:
    from tools.sim_harness.ha_stubs import install_ha_stubs

    install_ha_stubs()

from custom_components.climate_advisor import automation as auto  # noqa: E402
from custom_components.climate_advisor import i18n  # noqa: E402
from custom_components.climate_advisor.record_text import MsgText, msg_of  # noqa: E402
from custom_components.climate_advisor.whf_safety_decision import WhfSafetyAction, WhfSafetyDecision  # noqa: E402
from tools.sim_harness.ha_stubs import set_loaded_zones


@pytest.fixture(autouse=True)
def _catalogs():
    if not i18n.catalogs_loaded():
        i18n.load_catalogs()


@pytest.fixture
def fr_catalog(monkeypatch):
    def install(entries: dict[str, str]) -> None:
        cats = {lang: dict(c) for lang, c in i18n._CATALOGS.items()}
        cats.setdefault("fr", {}).update(entries)
        monkeypatch.setattr(i18n, "_CATALOGS", cats)

    return install


ENGLISH = [
    ("reason_whf_rule2_backstop", {}, "whole-house fan running — heating/cooling turned off (rule-2 backstop)"),
    (
        "reason_whf_suppress_newest_wins",
        {},
        "whole-house fan turned on after HVAC — suppressing HVAC (newest action wins)",
    ),
    ("reason_whf_stop_sealed", {}, "sealed house — stopping whole-house fan"),
    ("reason_whf_stop_hvac_on", {}, "HVAC turned on — stopping whole-house fan"),
    ("action_whf_stopped_rule2", {}, "Whole-house fan stopped (fan and heating/cooling were both running)"),
    ("action_hvac_off_whf_running", {}, "Heating/cooling turned off (whole-house fan running)"),
    ("action_whf_stop_failed", {}, "Whole-house fan could not be stopped"),
    ("reason_whf_confirmed_off_sealed", {}, "whole-house fan confirmed off after the sealed-house stop"),
    ("reason_door_window_arm_time", {}, "door/window open at comfort-band arm time"),
    ("reason_door_window_restore_time", {}, "door/window still open at comfort-restore time"),
    ("reason_door_window_open_entity", {"entity": "binary_sensor.front"}, "door/window open — binary_sensor.front"),
    ("reason_door_window_pause_entity", {"entity": "re-check"}, "door/window pause (re-check)"),
    ("reason_grace_expired_repause", {}, "grace expired — door/window still open, re-pausing"),
    (
        "reason_pause_was_mode",
        {"reason": "door/window open — x", "mode": "heat"},
        "door/window open — x, was heat mode",
    ),
    ("reason_user_resumed_door_window", {}, "user resumed from door/window pause"),
    ("reason_occupancy_home_restore", {"family": "heat"}, "occupancy home — restoring heat comfort"),
    (
        "reason_indoor_blind_stop_fan",
        {"minutes": 20},
        "indoor temperature unavailable for 20 min — stopping ventilation fan",
    ),
    ("action_stranded_hvac_restored", {}, "Restored HVAC mode from stranded fan suppression"),
    ("reason_stranded_hvac_restored", {"reason": "fan off", "mode": "cool"}, "fan off (restored=cool)"),
]


@pytest.mark.parametrize(("key", "params", "english"), ENGLISH)
def test_converted_literal_keeps_its_exact_english(key, params, english):
    text = auto._R(key, **params)
    assert isinstance(text, MsgText)
    assert text == english
    assert msg_of(text).key == f"activity.{key}"


def test_converted_literal_renders_in_another_language(fr_catalog):
    fr_catalog(
        {
            "activity.reason_door_window_open_entity": "porte/fenêtre ouverte — {entity}",
            "activity.reason_pause_was_mode": "{reason}, était en mode {mode}",
            "activity.reason_indoor_blind_stop_fan": "température intérieure indisponible depuis {minutes} min",
        }
    )
    inner = auto._R("reason_door_window_open_entity", entity="binary_sensor.front")
    outer = auto._R("reason_pause_was_mode", reason=auto._nest(inner), mode="heat")
    assert i18n.render(outer.msg, "fr") == "porte/fenêtre ouverte — binary_sensor.front, était en mode heat"
    blind = auto._R("reason_indoor_blind_stop_fan", minutes=20)
    assert i18n.render(blind.msg, "fr") == "température intérieure indisponible depuis 20 min"
    assert "door/window" not in i18n.render(outer.msg, "fr")
    # The record stays English whatever the display language.
    assert outer == "door/window open — binary_sensor.front, was heat mode"


# -- _record_action keeps the messages of a MsgText action/reason (a MsgText is a plain str param otherwise) ---------
def test_record_action_carries_the_messages_of_msgtext_arguments(fr_catalog):
    fr_catalog({"activity.action_whf_stop_failed": "Le ventilateur n'a pas pu être arrêté"})
    engine = MagicMock()
    auto.AutomationEngine._record_action(engine, auto._R("action_whf_stop_failed"), auto._R("reason_whf_stop_sealed"))
    assert engine._last_action_reason == (
        "Whole-house fan could not be stopped — sealed house — stopping whole-house fan"
    )
    wire = engine._last_action_reason_msg.to_wire()
    assert wire["params"]["action"]["key"] == "activity.action_whf_stop_failed"
    assert wire["params"]["reason"]["key"] == "activity.reason_whf_stop_sealed"
    fr = i18n.render(engine._last_action_reason_msg, "fr")
    assert fr.startswith("Le ventilateur n'a pas pu être arrêté")


def test_record_action_plain_strings_still_work():
    engine = MagicMock()
    auto.AutomationEngine._record_action(engine, "Set HVAC to cool", "because")
    assert engine._last_action_reason == "Set HVAC to cool — because"
    assert engine._last_action_reason_msg.to_wire()["params"] == {"action": "Set HVAC to cool", "reason": "because"}


# -- sinks ----------------------------------------------------------------------------------------------------------
def _decision(stops_fan: bool) -> WhfSafetyDecision:
    msg = i18n.Msg("notify.whf_reason_sealed_stop")
    return WhfSafetyDecision(
        WhfSafetyAction.STOP_MUTEX_FAN if stops_fan else WhfSafetyAction.SUPPRESS_HVAC,
        i18n.render_en(msg),
        reason_msg=msg,
        backstop=True,
    )


@pytest.mark.parametrize(
    ("stops_fan", "action"),
    [
        (True, "Whole-house fan stopped (fan and heating/cooling were both running)"),
        (False, "Heating/cooling turned off (whole-house fan running)"),
    ],
)
def test_rule2_backstop_row_is_messages_both_sides(stops_fan, action):
    """Mirror pair: the fan-stop and the HVAC-off rows are both messages carrying the decision reason's Msg."""
    engine = MagicMock()
    engine._emit_event_callback = None
    engine._recent_duplicate.return_value = True
    snap = MagicMock(fan_on_for_s=1.0)
    auto.AutomationEngine._announce_whf_rule2_backstop(
        engine, _decision(stops_fan), trigger="t", snap=snap, climate_state=MagicMock(state="heat")
    )
    act, reason = engine._record_action.call_args.args
    assert act == action and msg_of(act) is not None
    assert reason == _decision(stops_fan).reason
    assert msg_of(reason).key == "notify.whf_reason_sealed_stop"


def test_stop_failed_row_is_a_message():
    engine = MagicMock()
    engine._whf_last_command = ("off", 0, False)
    engine._recent_duplicate.return_value = True
    auto.AutomationEngine._alert_whf_stop_failed(engine, trigger="t", attempts=2)
    act, reason = engine._record_action.call_args.args
    assert act == "Whole-house fan could not be stopped"
    assert msg_of(act).key == "activity.action_whf_stop_failed"
    assert msg_of(reason).key == "notify.whf_stop_failed_detail_control_off"
    assert reason == i18n.render_en(msg_of(reason))


def test_door_window_pause_reasons_are_messages():
    engine = MagicMock()
    engine.hass.states.get.return_value = MagicMock(state="heat")
    engine._pre_pause_mode = None
    engine._set_hvac_mode = AsyncMock(return_value=auto.HvacCommandResult.EXECUTED)
    engine._notify = AsyncMock()
    engine._emit_event_callback = None
    result = asyncio.run(
        auto.AutomationEngine._pause_for_door_window_action(
            engine,
            entity_label="front",
            reason=auto._R("reason_door_window_open_entity", entity="front"),
            notify_message=i18n.Msg("notify.door_window_paused"),
            notify_type="door_window_pause",
        )
    )
    assert result is False
    set_reason = engine._set_hvac_mode.call_args.kwargs["reason"]
    assert set_reason == "door/window open — front, was heat mode"
    inner = msg_of(set_reason).params["reason"]
    assert isinstance(inner, i18n.Msg) and inner.key == "activity.reason_door_window_open_entity"
    retry_reason = engine._arm_open_window_off_retry.call_args.kwargs["reason"]
    assert retry_reason == "door/window pause (front)"
    assert msg_of(retry_reason).key == "activity.reason_door_window_pause_entity"


def test_indoor_blind_stop_reason_is_a_message():
    engine = MagicMock()
    engine.config = {}
    engine._exit_nat_vent = AsyncMock(return_value=auto.FanCommandResult.DRY_RUN)
    asyncio.run(auto.AutomationEngine._indoor_blind_stop(engine, 20 * 60 + 5))
    reason = engine._exit_nat_vent.call_args.kwargs["reason"]
    assert reason == "indoor temperature unavailable for 20 min — stopping ventilation fan"
    assert msg_of(reason).params == {"minutes": 20}


def test_the_unused_phrase_helpers_are_gone():
    assert not hasattr(auto.AutomationEngine, "_outdoor_ge_indoor")
    assert not hasattr(auto.AutomationEngine, "_indoor_le_comfort_floor")


# -- API ------------------------------------------------------------------------------------------------------------
def _request(coordinator, hours: str | None = None) -> MagicMock:

    hass = MagicMock()
    set_loaded_zones(hass, {"entry1": coordinator})
    hass.config.language = "fr"
    req = MagicMock()
    req.app = {"hass": hass}
    query = {"hours": hours} if hours is not None else {}
    req.query = query
    req.rel_url = MagicMock()
    req.rel_url.query = query
    return req


def test_learning_suggestions_carry_text_msg_and_render_in_hass_language():
    from custom_components.climate_advisor.api import ClimateAdvisorLearningView

    msg = i18n.Msg("learning.suggestion.low_window_compliance", pct=40)
    coordinator = MagicMock()
    coordinator.config = {}
    coordinator.today_record = None
    coordinator.tomorrow_plan = None
    coordinator.learning.generate_suggestions.return_value = ["text"]
    coordinator.learning.get_last_suggestion_keys.return_value = ["low_window_compliance"]
    coordinator.learning.get_last_suggestion_msgs.return_value = [msg]
    body = asyncio.run(ClimateAdvisorLearningView().get(_request(coordinator))).json_data
    coordinator.learning.generate_suggestions.assert_called_once_with(lang="fr")
    assert body["suggestions"] == [{"key": "low_window_compliance", "text": "text", "text_msg": msg.to_wire()}]
    assert i18n.Msg.from_wire(body["suggestions"][0]["text_msg"]) == msg


def test_activity_record_adds_rows_with_msg_siblings_and_keeps_table():
    from homeassistant.util import dt as dt_util

    from custom_components.climate_advisor.api import ClimateAdvisorActivityRecordView

    now = datetime.datetime(2026, 6, 17, 14, 0, tzinfo=datetime.UTC)
    coordinator = MagicMock()
    coordinator.config = {"temp_unit": "fahrenheit"}
    coordinator._event_log = [
        {
            "time": (now - datetime.timedelta(hours=1)).isoformat(),
            "type": "fan_cancel",
            "fan_before": "on",
            "fan_after": "off",
        }
    ]
    with patch.object(dt_util, "now", return_value=now):
        body = asyncio.run(ClimateAdvisorActivityRecordView().get(_request(coordinator, "12"))).json_data
    assert {"table", "hours", "generated_at", "is_truncated", "oldest_available"} <= set(body)
    assert "Fan cancel (user turned off)" in body["table"]
    (row,) = body["rows"]
    assert row["event"].startswith("Fan cancel")
    assert i18n.render(i18n.Msg.from_wire(row["event_msg"]), "en") == row["event"]
    assert i18n.render(i18n.Msg.from_wire(row["settings_msg"]), "en") == row["settings"] == "fan: on->off"
    assert body["limited_msg"] is None and body["no_events_msg"] is None


# -- the suggestions sensor ------------------------------------------------------------------------------------------
def test_compliance_sensor_renders_suggestions_in_hass_language(fr_catalog):
    from custom_components.climate_advisor import sensor as sensor_mod

    fr_catalog({"learning.suggestion.low_window_compliance": "Seulement {pct} % des fenêtres"})
    msg = i18n.Msg("learning.suggestion.low_window_compliance", pct=40)
    s = object.__new__(sensor_mod.ClimateAdvisorComplianceSensor)
    s.hass = MagicMock()
    s.hass.config.language = "fr"
    coordinator = MagicMock()
    coordinator.config = {}
    coordinator.data = {
        sensor_mod.ATTR_LEARNING_SUGGESTIONS: ["English text"],
        sensor_mod.ATTR_LEARNING_SUGGESTION_MSGS: [msg],
    }
    coordinator.today_record = None
    coordinator.learning.get_thermal_model.return_value = {}
    coordinator.learning.get_weather_bias.return_value = {}
    s.coordinator = coordinator
    attrs = s.extra_state_attributes
    assert attrs["pending_suggestions"] == 1
    assert attrs["suggestions"] == ["Seulement 40 % des fenêtres"]
    assert attrs["suggestion_codes"] == [{"code": msg.key, "params": msg.to_wire()["params"]}]
