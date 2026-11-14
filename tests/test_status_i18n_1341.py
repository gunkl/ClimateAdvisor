"""Issue #1341 (L4): status text, API messages and the codes the code compares are catalog messages.

Occupant view: the Status card, the Next Action / Next Automation cards, the fan and contact lines, the pre-cool line,
the remote-offset sentence and every API reply show the same words as before in English, and in another language
render from the catalog with the typed values (temperatures, clock times, durations) filled in -- no English left.
The non-English catalogs are written later by the translators, so each test here installs a stub pattern for the
language it renders in (``translated``) and checks the sink really goes through the catalog.
"""

from __future__ import annotations

import asyncio
import copy
import pickle
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.climate_advisor import api as api_mod
from custom_components.climate_advisor import i18n
from custom_components.climate_advisor.automation import (
    BAND_CONTEXT_AWAY,
    BAND_CONTEXT_COMFORT,
    BAND_CONTEXT_TOU_EXPANSION,
    ComfortBand,
    CurrentHold,
    TouBankHold,
    hold_after_msg,
    hold_after_text,
    hold_band_label,
    hold_sentence,
    hold_sentence_msg,
    tou_bank_band,
)
from custom_components.climate_advisor.const import OCCUPANCY_AWAY
from custom_components.climate_advisor.coordinator import ClimateAdvisorCoordinator
from custom_components.climate_advisor.remote_offset import (
    ClampLimit,
    RemoteLoopState,
    RemoteOffsetState,
    remote_offset_msg,
    remote_offset_text,
    remote_offset_view,
)
from custom_components.climate_advisor.status_text import (
    FAN_STATUS_CODES,
    CodedText,
    code_of,
    fan_status_text,
    msg_of,
    render_text,
    wire_of,
)
from tests.test_status_sensors import _compute_automation_status, _make_automation_engine

NOW = datetime(2026, 7, 13, 14, 0, tzinfo=UTC)
PEAK = datetime(2026, 7, 13, 16, 15, tzinfo=UTC)
PEAK_END = datetime(2026, 7, 13, 21, 0, tzinfo=UTC)


@pytest.fixture
def translated(monkeypatch):
    """Install stub German patterns: ``translated({key: pattern})`` (the real translations arrive later)."""

    def _put(patterns: dict[str, str]) -> None:
        for key, pattern in patterns.items():
            monkeypatch.setitem(i18n._CATALOGS["de"], key, pattern)

    return _put


def _wire_round_trip(msg: i18n.Msg) -> i18n.Msg:
    return i18n.Msg.from_wire(msg.to_wire())


# -- CodedText -------------------------------------------------------------------------------------------------
class TestCodedText:
    def test_is_the_english_string_with_a_code_and_a_message(self) -> None:
        text = CodedText("all_closed", i18n.Msg("status.contact.all_closed"))
        assert text == "all closed" and isinstance(text, str)
        assert code_of(text) == "all_closed"
        assert msg_of(text) == i18n.Msg("status.contact.all_closed")
        assert wire_of(text) == {"key": "status.contact.all_closed", "params": {}}

    def test_plain_text_has_no_code_or_message(self) -> None:
        assert code_of("active") is None and msg_of("active") is None and wire_of("active") is None
        assert render_text("active", "de") == "active"

    def test_survives_pickle_and_deepcopy(self) -> None:
        text = CodedText("clock", i18n.Msg("status.clock", t=i18n.Time(NOW)))
        for clone in (copy.deepcopy(text), pickle.loads(pickle.dumps(text))):
            assert clone == text and clone.code == "clock" and clone.msg == text.msg

    def test_renders_in_another_language(self, translated) -> None:
        translated({"status.contact.all_closed": "alle geschlossen"})
        text = CodedText("all_closed", i18n.Msg("status.contact.all_closed"))
        assert render_text(text, "de") == "alle geschlossen"
        assert render_text(text, "en") == "all closed"

    @pytest.mark.parametrize(
        ("token", "code"),
        [
            ("disabled", "disabled"),
            ("active (unconfirmed)", "active_unconfirmed"),
            ("running (manual override)", "running_manual_override"),
            ("nat-vent (session active, fan idle)", "nat_vent_idle"),
        ],
    )
    def test_fan_tokens_keep_their_text_and_gain_a_code(self, token: str, code: str) -> None:
        text = fan_status_text(token)
        assert text == token and text.code == code
        assert i18n.render_en(text.msg) == token  # the catalog English IS the token

    def test_every_fan_token_is_its_own_catalog_english(self) -> None:
        for token, (_code, key) in FAN_STATUS_CODES.items():
            assert i18n.render_en(i18n.Msg(key)) == token


# -- the bank sentence (hold_* helpers) ------------------------------------------------------------------------
def _band(floor: float, ceiling: float, context: str = BAND_CONTEXT_COMFORT, active: str = "ceiling") -> ComfortBand:
    return ComfortBand(floor=floor, ceiling=ceiling, active=active, reason="t", context=context)


def _away_hold(*, widened: bool = False) -> CurrentHold:
    schedule = _band(60.0, 80.0, BAND_CONTEXT_AWAY)
    bank = TouBankHold(target=77.0, mode="cool", ends=PEAK, period_end=PEAK_END if widened else None)
    after = _band(60.0, 83.0, BAND_CONTEXT_TOU_EXPANSION) if widened else schedule
    return CurrentHold(tou_bank_band(schedule, 77.0, "cool"), schedule, bank, after, OCCUPANCY_AWAY)


class TestHoldMessages:
    def test_english_is_unchanged(self) -> None:
        assert hold_sentence(_away_hold(), "fahrenheit") == (
            "Pre-cooling to 77°F until the 4:15 PM high-cost period, then back to the Away band 60–80°F"
        )
        assert hold_after_text(_away_hold(widened=True), "fahrenheit") == (
            "the Away band widened to 60–83°F until 9:00 PM"
        )
        assert hold_band_label(_band(60.0, 80.0, BAND_CONTEXT_AWAY), "away") == "Away band"

    def test_celsius_params_are_typed_not_preformatted(self) -> None:
        msg = hold_sentence_msg(_away_hold(), "celsius")
        wire = msg.to_wire()
        assert wire["params"]["target"] == {"$t": "temp", "f": 77.0, "unit": "celsius", "d": 0}
        assert wire["params"]["ends"]["$t"] == "time"
        assert i18n.render_en(_wire_round_trip(msg)) == (
            "Pre-cooling to 25°C until the 4:15 PM high-cost period, then back to the Away band 16–27°C"
        )

    def test_lowercase_variant_is_its_own_message(self) -> None:
        lower = hold_sentence_msg(_away_hold(), "fahrenheit", lowercase=True)
        assert lower.key == "status.hold.sentence_lc"
        assert i18n.render_en(lower).startswith("pre-cooling to 77°F")

    def test_renders_in_another_language_with_values_substituted(self, translated) -> None:
        translated(
            {
                "status.hold.sentence": (
                    "{mode, select, cool {Vorkühlen} other {Vorheizen}} auf {target} bis {ends}: {after}"
                ),
                "status.hold.after_back": "zurück zum {label} {range}",
                "status.hold.band_away": "Abwesenheitsband",
                "status.hold.range": "{lo, number}–{hi}",
            }
        )
        text = i18n.render(hold_sentence_msg(_away_hold(), "fahrenheit"), "de")
        assert text == "Vorkühlen auf 77°F bis 16:15: zurück zum Abwesenheitsband 60–80°F"  # de clock: 24-hour
        assert "Pre-cooling" not in text and "high-cost" not in text

    def test_after_is_none_without_a_bank(self) -> None:
        schedule = _band(68.0, 76.0)
        assert hold_after_msg(CurrentHold(schedule, schedule), "fahrenheit") is None


# -- automation status codes -----------------------------------------------------------------------------------
class TestAutomationStatus:
    def test_codes_and_english(self) -> None:
        for kwargs, code, text in [
            ({}, "active", "active"),
            ({"natural_vent_active": True}, "nat_vent", "nat-vent"),
        ]:
            engine = _make_automation_engine(**kwargs)
            engine._nat_vent_soft_start = False  # a bare MagicMock attribute would read as truthy
            status = _compute_automation_status(True, engine)
            assert status == text and code_of(status) == code, (kwargs, status)
        disabled = _compute_automation_status(False, _make_automation_engine())
        assert disabled == "disabled" and code_of(disabled) == "disabled"

    def _grace_engine(self):
        return _make_automation_engine(
            grace_active=True,
            last_resume_source="manual",
            last_grace_trigger="fan_off",
            grace_end_time=NOW.replace(hour=15).isoformat(),
            grace_duration_seconds=3600,
        )

    def test_grace_period_carries_the_cause_and_the_clock_as_typed_params(self) -> None:
        status = _compute_automation_status(True, self._grace_engine(), now_dt=NOW)
        assert code_of(status) == "grace_period"
        assert status == "grace period (manual) — WHF turned off — 1h (ends 3:00 PM)"
        params = status.msg.to_wire()["params"]
        assert params["source"] == "manual"
        detail = params["detail"]  # a nested Msg, never a joined sentence
        assert detail["$t"] == "msg" and detail["key"] == "status.part.grace_detail"
        remaining = detail["params"]["remaining"]
        assert remaining["params"]["dur"] == {"$t": "dur", "s": 3600}
        assert remaining["params"]["t"]["$t"] == "time"

    def test_grace_period_renders_in_another_language(self, translated) -> None:
        translated(
            {
                "status.auto.grace_period": (
                    "Schonfrist ({source, select, manual {manuell} other {{source}}}){detail}"
                ),
                "status.grace_cause.fan_off": "Ventilator aus",
                "status.part.remaining": " — {dur} (bis {t})",
            }
        )
        status = _compute_automation_status(True, self._grace_engine(), now_dt=NOW)
        assert (
            render_text(status, "de") == "Schonfrist (manuell) — Ventilator aus — 1 Std. (bis 15:00)"
        )  # de duration and clock

    def test_the_fan_request_note_is_added_once(self) -> None:
        engine = _make_automation_engine(
            grace_active=True, last_resume_source="automation", last_grace_trigger="override_confirmed"
        )
        engine._fan_off_hold_until = NOW.replace(hour=16)
        engine._fan_override_until = None
        engine._fan_override_active = False
        status = _compute_automation_status(True, engine, now_dt=NOW)
        assert str(status).count("fan held off until 4:00 PM") == 1


# -- contact status --------------------------------------------------------------------------------------------
def _contact_coordinator(open_ids: set[str], dark: set[str], held: set[str], sensors: list[str]):
    coord = MagicMock()
    coord._resolved_sensors = sensors
    coord._is_sensor_open = lambda s: s in open_ids
    coord._sensor_real_reading = lambda state: None if state in dark else "on"
    coord.hass.states.get = lambda s: s
    coord._sensor_holds_open = lambda s: s in held
    return coord


class TestContactStatus:
    def test_codes_and_english(self) -> None:
        compute = ClimateAdvisorCoordinator._compute_contact_status
        none = compute(_contact_coordinator(set(), set(), set(), []))
        assert (none, code_of(none)) == ("no sensors", "no_sensors")
        closed = compute(_contact_coordinator(set(), set(), set(), ["a", "b"]))
        assert (closed, code_of(closed)) == ("all closed", "all_closed")
        mixed = compute(_contact_coordinator({"a"}, {"b", "c"}, {"b"}, ["a", "b", "c"]))
        assert mixed == "1 open, 2 not responding (1 last read open, held open)" and code_of(mixed) == "counts"

    def test_counts_render_in_another_language(self, translated) -> None:
        translated(
            {
                "status.contact.open": "{n} offen",
                "status.contact.dark": "{n} antworten nicht",
                "status.contact.join": "{a}; {b}",
            }
        )
        status = ClimateAdvisorCoordinator._compute_contact_status(
            _contact_coordinator({"a"}, {"b"}, set(), ["a", "b"])
        )
        assert render_text(status, "de") == "1 offen; 1 antworten nicht"


# -- pre-cool: a code, not English prefixes --------------------------------------------------------------------
class TestPreCool:
    def _run(self, result):
        coord = MagicMock()
        coord.automation_engine = SimpleNamespace(
            natural_vent_active=False, handle_pre_cool=AsyncMock(return_value=result)
        )
        coord._get_indoor_temp = MagicMock(return_value=76.0)
        coord.async_refresh = AsyncMock()
        coord._pre_cool_trigger_dt = object()
        coord._pre_cool_modifier_for_night = MagicMock(return_value=-2.0)
        coord._pre_cool_status = "unchanged"
        asyncio.run(ClimateAdvisorCoordinator._async_pre_cool_trigger(coord, NOW))
        return coord._pre_cool_status

    @staticmethod
    def _result(code: str, key: str, **params):
        return CodedText(code, i18n.Msg(key, **params))

    def test_not_applied_is_never_presented_as_active(self) -> None:
        result = self._result(
            "not_applied", "status.pre_cool.not_applied", outcome="blocked", target=i18n.Temp(72.0, "fahrenheit", 1)
        )
        assert result == "not applied (blocked): 72.0°F"
        status = self._run(result)
        assert status == "pre-cool not applied (blocked): 72.0°F" and status.code == "not_applied"

    def test_applied_and_suppressed(self) -> None:
        target = i18n.Temp(72.0, "fahrenheit", 1)
        applied = self._run(self._result("applied", "status.pre_cool.applied", target=target))
        assert applied == "pre-cool active (72.0°F)" and applied.code == "active"
        session = self._run(self._result("suppressed_session", "status.pre_cool.suppressed_session"))
        assert session == "pre-cool suppressed — nat-vent/WHF session active" and session.code == "suppressed"
        achieved = self._run(
            self._result(
                "suppressed_achieved",
                "status.pre_cool.suppressed_achieved",
                indoor=i18n.Temp(70.0, "fahrenheit", 1),
                target=target,
            )
        )
        assert achieved == "pre-cool suppressed — nat-vent achieved 70.0°F (target 72.0°F)"

    def test_a_skipped_result_leaves_the_status_alone(self) -> None:
        skipped = self._run(self._result("skipped_awake", "status.pre_cool.skipped_awake"))
        assert skipped == "unchanged"

    def test_the_code_decides_not_the_words(self) -> None:
        """English that happens to say 'suppressed' under the code ``skipped_*`` changes nothing."""
        tricky = CodedText("skipped_not_eligible", None, text="suppressed: applied (not applied)")
        assert self._run(tricky) == "unchanged"

    def test_status_renders_in_another_language(self, translated) -> None:
        translated({"status.pre_cool_status.active": "Vorkühlung aktiv ({target})"})
        status = self._run(self._result("applied", "status.pre_cool.applied", target=i18n.Temp(71.6, "celsius", 1)))
        assert render_text(status, "de") == "Vorkühlung aktiv (22,0°C)"


# -- remote offset sentences -----------------------------------------------------------------------------------
def _offset_state(**overrides) -> RemoteOffsetState:
    base = {
        "state": RemoteLoopState.CLAMPED,
        "context": "sleep",
        "mode": "heat",
        "comfort_target_f": 66.0,
        "commanded_f": 70.0,
        "offset_f": 4.0,
        "gate_reason": None,
        "clamp_limit": ClampLimit.CAP,
        "last_reason": "at_cap",
        "remote_value_f": 64.0,
        "remote_age_s": 30.0,
        "cap_limit_f": 4.0,
    }
    base.update(overrides)
    return RemoteOffsetState(**base)


class TestRemoteOffset:
    def test_english_sentence_is_unchanged(self) -> None:
        assert remote_offset_text(_offset_state(), "fahrenheit") == (
            "Bedroom 66°F target - thermostat set 70°F (4° above the target, clamped at the 4° cap)"
        )

    def test_view_carries_the_prose_and_the_wire_message(self) -> None:
        view = remote_offset_view(_offset_state(), "fahrenheit")
        assert view["text"].startswith("Bedroom 66°F target")
        wire = view["text_msg"]
        assert wire["key"] == "status.remote_offset.main"
        assert wire["params"]["target"] == {"$t": "temp", "f": 66.0, "unit": "fahrenheit", "d": 0}
        assert view["context_label_msg"] == {"key": "status.remote_offset.label_bedroom", "params": {}}
        assert i18n.render_en(i18n.Msg.from_wire(wire)) == view["text"]

    def test_renders_in_another_language(self, translated) -> None:
        translated(
            {
                "status.remote_offset.main": "{label}: Ziel {target}, Thermostat {commanded} ({amount}, {note})",
                "status.remote_offset.label_bedroom": "Schlafzimmer",
                "status.remote_offset.delta_above": "{gap} über dem Ziel",
                "status.remote_offset.note_clamped_cap": "bei {gap} begrenzt",
            }
        )
        view = remote_offset_view(_offset_state(), "fahrenheit", "de")
        assert view["text"] == "Schlafzimmer: Ziel 66°F, Thermostat 70°F (4° über dem Ziel, bei 4° begrenzt)"
        assert view["context_label"] == "Schlafzimmer"

    def test_gate_reasons_and_inactive(self) -> None:
        gated = _offset_state(state=RemoteLoopState.GATED, gate_reason="door_window_pause")
        assert remote_offset_text(gated, "fahrenheit") == "Bedroom offset off: paused for an open window or door"
        assert remote_offset_msg(_offset_state(state=RemoteLoopState.INACTIVE), "fahrenheit") is None

    def test_unknown_gate_reason_keeps_its_raw_text(self) -> None:
        gated = _offset_state(state=RemoteLoopState.GATED, gate_reason="something_new")
        assert remote_offset_text(gated, "fahrenheit") == "Bedroom offset off: something_new"


# -- API messages and errors -----------------------------------------------------------------------------------
class TestApiMessages:
    def test_error_body_has_prose_code_and_wire(self) -> None:
        hass = SimpleNamespace(config=SimpleNamespace(language="en"))
        assert api_mod._error_body(hass, "not_loaded") == {
            "error": "Climate Advisor not loaded",
            "error_msg": {"key": "errors.not_loaded", "params": {}},
            "error_code": "not_loaded",
        }

    def test_error_renders_in_the_backend_language(self, translated) -> None:
        translated({"errors.not_loaded": "Climate Advisor nicht geladen"})
        hass = SimpleNamespace(config=SimpleNamespace(language="de"))
        body = api_mod._error_body(hass, "not_loaded")
        assert body["error"] == "Climate Advisor nicht geladen"
        assert body["error_msg"]["key"] == "errors.not_loaded"

    def test_a_coded_error_keeps_its_code_and_gains_the_siblings(self) -> None:
        hass = SimpleNamespace(config=SimpleNamespace(language="en"))
        extra = api_mod._coded_extra(hass, "github_api", status=422)
        assert extra["error_code"] == "github_api"
        assert extra["error_text"] == "GitHub returned an error (HTTP 422)."
        assert extra["error_msg"]["params"] == {"status": 422}

    def test_ambiguous_zone_message_is_unchanged_in_english(self) -> None:
        hass = MagicMock()
        hass.config.language = "en"
        with patch.object(api_mod.zone_registry, "list_zones", return_value=[{"entry_id": "a"}, {"entry_id": "b"}]):
            body = api_mod._ambiguous_zone_payload(hass)
        assert body["message"] == (
            "Multiple Climate Advisor zones are configured — add ?zone=<entry_id> (one of the entry_id values in "
            "'zones') so the request names exactly one zone."
        )
        assert body["message_msg"]["key"] == "errors.zone_selection_required"
        assert body["zone_count"] == 2

    @pytest.mark.parametrize(
        ("what", "english"),
        [
            ("override", "Override cancelled. Automated control resumes in 10 seconds."),
            ("hold", "Fan-off pause ended. Automated control resumes in 10 seconds."),
            ("grace", "Grace period ended. Automated control resumes in 10 seconds."),
        ],
    )
    def test_cancel_message_variants(self, what, english) -> None:
        assert i18n.render_en(i18n.Msg("errors.cancel_done", what=what)) == english

    @pytest.mark.parametrize(
        ("mode", "english"),
        [
            ("cool", "Resumed from pause. HVAC set to cool. Manual grace period started."),
            ("fan_only", "Resumed from pause. HVAC set to fan_only. Manual grace period started."),
            ("none", "Resumed from pause. HVAC set to N/A. Manual grace period started."),
        ],
    )
    def test_resume_message(self, mode, english) -> None:
        assert i18n.render_en(i18n.Msg("errors.resume_done", mode=mode)) == english

    def test_resume_pending_without_a_mode(self) -> None:
        assert i18n.render_en(i18n.Msg("errors.resume_pending", mode="none")).startswith(
            "Pause cleared. The thermostat did not take the restore yet;"
        )

    def test_text_fields_render_the_prose_and_keep_the_code(self, translated) -> None:
        translated({"status.auto.active": "aktiv"})
        status = CodedText("active", i18n.Msg("status.auto.active"))
        out = api_mod._text_fields("automation_status", status, "de")
        assert out == {
            "automation_status": "aktiv",
            "automation_status_msg": {"key": "status.auto.active", "params": {}},
        }
        coded = api_mod._text_fields("fan_status", fan_status_text("inactive"), None, code=True)
        assert coded["fan_status"] == "inactive" and coded["fan_status_code"] == "inactive"
        plain = api_mod._text_fields("next_action", "-", "de", code=True)
        assert plain == {"next_action": "-", "next_action_msg": None, "next_action_code": None}


# -- _record_action --------------------------------------------------------------------------------------------
class TestRecordAction:
    def test_keeps_the_english_record_and_the_message(self) -> None:
        from custom_components.climate_advisor.automation import AutomationEngine

        engine = MagicMock()
        AutomationEngine._record_action(engine, "Set HVAC to cool", i18n.Msg("status.pre_cool.skipped_awake"))
        assert engine._last_action_reason == "Set HVAC to cool — skipped: awake hours"
        assert engine._last_action_reason_msg.key == "status.action_reason"
        assert engine._last_action_reason_msg.to_wire()["params"]["reason"]["key"] == "status.pre_cool.skipped_awake"

    def test_a_plain_reason_still_works(self) -> None:
        from custom_components.climate_advisor.automation import AutomationEngine

        engine = MagicMock()
        AutomationEngine._record_action(engine, "Set temp to 70°F (mode=cool)", "mode switch before setpoint write")
        assert engine._last_action_reason == "Set temp to 70°F (mode=cool) — mode switch before setpoint write"

    def test_debug_state_prose_follows_the_backend_language(self, translated) -> None:
        translated({"status.action_reason": "{action} — Grund: {reason}"})
        coord = object.__new__(ClimateAdvisorCoordinator)
        coord.hass = SimpleNamespace(config=SimpleNamespace(language="de"))
        engine = SimpleNamespace(
            _last_action_reason="Set HVAC to cool — x",
            _last_action_reason_msg=i18n.Msg("status.action_reason", action="Set HVAC to cool", reason="x"),
        )
        assert coord._last_action_reason_text(engine) == "Set HVAC to cool — Grund: x"
        assert coord._last_action_reason_wire(engine)["key"] == "status.action_reason"
        legacy = SimpleNamespace(_last_action_reason="restored text")
        assert coord._last_action_reason_text(legacy) == "restored text"
        assert coord._last_action_reason_wire(legacy) is None


# -- sensor roles, source reasons, away / vacation lines -------------------------------------------------------
class TestSmallSinks:
    def test_sensor_meta_has_role_code_and_message(self) -> None:
        coord = object.__new__(ClimateAdvisorCoordinator)
        coord.hass = SimpleNamespace(config=SimpleNamespace(language="en"), states=SimpleNamespace(get=lambda _e: None))
        coord.config = {"climate_entity": "climate.t", "sleep_indoor_temp_entity": "sensor.bed"}
        coord._indoor_companion_in_use = "sensor.finer"
        coord._v4_store = None
        ids = ["climate.t", "sensor.bed", "sensor.finer", "sensor.x"]
        rows = {r["id"]: r for r in coord.v4_indoor_sensors_meta(ids)}
        assert (rows["climate.t"]["role"], rows["climate.t"]["role_code"]) == ("thermostat", "thermostat")
        assert rows["sensor.bed"]["role"] == "bedroom sensor"
        assert rows["sensor.finer"]["role_code"] == "finer_sensor"
        assert rows["sensor.x"]["role_msg"] == {"key": "status.sensor_role.room_sensor", "params": {}}

    def test_away_and_vacation_pools_are_catalog_messages(self) -> None:
        from custom_components.climate_advisor import coordinator as mod

        assert len(mod._AWAY_ACTION_MESSAGES) == 10 and len(mod._VACATION_ACTION_MESSAGES) == 10
        picked = mod._pick_daily_line(mod._AWAY_ACTION_MESSAGES, "away")
        assert isinstance(picked, i18n.Msg) and i18n.render_en(picked)
        assert i18n.render_en(mod._AWAY_ACTION_MESSAGES[0]) == "You're away. The house is holding steady."

    def test_grace_trigger_labels_are_catalog_keys_with_the_old_words(self) -> None:
        from custom_components.climate_advisor import coordinator as mod

        words = {k: i18n.render_en(i18n.Msg(v)) for k, v in mod._GRACE_TRIGGER_LABELS.items()}
        assert words["fan_off"] == "WHF turned off" and words["sensor_closed_resume"] == "door/window closed"

    def test_indoor_source_reasons(self) -> None:
        coord = object.__new__(ClimateAdvisorCoordinator)
        coord._indoor_source_left_sleep_failed = False
        coord._indoor_source_companions_seen = set()
        reading = SimpleNamespace(
            remote_context=None,
            sleep_reason=None,
            companion_entity="sensor.finer",
            companion_reason="inconsistent",
        )
        coord._companion_for_resolver = lambda: object()
        msg = coord._indoor_source_reason(reading, "climate.t", "finer_sensor")
        assert i18n.render_en(msg) == "more than 1.0 °F apart"
        reading.companion_reason = "unavailable"
        assert i18n.render_en(coord._indoor_source_reason(reading, "climate.t", "finer_sensor")) == (
            "finer sensor unavailable — using the thermostat"
        )
        assert i18n.render_en(coord._indoor_source_reason(reading, "sensor.finer", "thermostat")) == (
            "finer sensor proven"
        )

    def test_comfort_hold_message(self) -> None:
        ae = SimpleNamespace(
            _last_comfort_hold={
                "candidate_family": "cooling",
                "depth_f": 1.5,
                "tolerance_f": 1.0,
                "opposite_ran_min_ago": 20,
                "window_min": 90,
                "until_ts": NOW.replace(hour=15, minute=10).isoformat(),
                "zone": "past",
            }
        )
        with patch("custom_components.climate_advisor.coordinator.dt_util.as_local", side_effect=lambda x: x):
            text = ClimateAdvisorCoordinator._comfort_family_hold_label(ae, NOW, "fahrenheit")
            msg = ClimateAdvisorCoordinator._comfort_family_hold_msg(ae, NOW, "fahrenheit")
        assert text == "holding cooling: 1.5°F past ceiling, tolerance 1.0°F, heating ran 20 min ago (until ~3:10 PM)"
        assert msg.to_wire()["params"]["until"]["$t"] == "time"

    def test_thermostat_notice_text_is_a_message(self) -> None:
        coord = object.__new__(ClimateAdvisorCoordinator)
        coord.config = {"climate_entity": "climate.t"}
        coord._hvac_report_watch = {"entity": "climate.t", "ever_reported": False, "unreported_s": 10**9}
        notice = coord.thermostat_action_notice()
        assert notice["rate_in_use"] == "default"
        assert notice["text"].startswith("Your thermostat has not reported when it is heating or cooling")
        assert notice["text_msg"]["key"] == "status.thermostat_notice.text"
        assert notice["text_msg"]["params"]["use"]["key"] == "status.thermostat_notice.use_defaults"


# -- the catalog and the code agree ----------------------------------------------------------------------------
_PKG = __import__("pathlib").Path(__file__).resolve().parent.parent / "custom_components" / "climate_advisor"
_SOURCES = ("coordinator.py", "automation.py", "api.py", "dashboard_api.py", "remote_offset.py", "status_text.py")


def _catalog_en() -> dict[str, str]:
    import json

    merged: dict[str, str] = {}
    for area in ("status", "errors"):
        merged.update(json.loads((_PKG / "frontend" / "i18n" / area / "en.json").read_text(encoding="utf-8")))
    return merged


class TestCatalogAndCodeAgree:
    def test_every_message_parses_in_the_icu_subset(self) -> None:
        for pattern in _catalog_en().values():
            i18n.parse_message(pattern)  # raises on anything outside the subset

    def test_every_key_the_code_names_exists_and_none_is_orphaned(self) -> None:
        import re

        pattern = re.compile(r"""["']((?:status|errors)\.[a-z0-9_]+(?:\.[a-z0-9_]+)*)["']""")
        used: set[str] = set()
        for name in _SOURCES:
            used.update(pattern.findall((_PKG / name).read_text(encoding="utf-8")))
        catalog = set(_catalog_en())
        missing = sorted(k for k in used if k not in catalog)
        assert not missing, f"keys used in code but absent from en.json: {missing}"
        # keys built from a prefix in code (the away / vacation line pools are listed one by one, so none are dynamic)
        orphans = sorted(k for k in catalog if k not in used)
        assert not orphans, f"en.json keys no code names: {orphans}"


# -- payload rows carry the wire siblings of messages other lanes attach -----------------------------------------
class TestPayloadRowSiblings:
    def test_rows_without_messages_get_none(self) -> None:
        from custom_components.climate_advisor.coordinator import _entity_health_rows, _invariant_rows

        v = SimpleNamespace(name="ac_whf", detail="both on")
        assert _invariant_rows([v]) == [{"invariant": "ac_whf", "detail": "both on", "detail_msg": None}]
        issue = SimpleNamespace(
            config_key="fan", entity_id="fan.x", friendly_name="Fan", criticality="high", status="missing", detail="d"
        )
        row = _entity_health_rows([issue])[0]
        assert row["friendly_name_msg"] is None and row["detail_msg"] is None and row["detail"] == "d"

    def test_rows_with_messages_carry_the_wire_form(self) -> None:
        from custom_components.climate_advisor.coordinator import _entity_health_rows, _invariant_rows

        msg = i18n.Msg("status.clock", t=i18n.Time(NOW))
        v = SimpleNamespace(name="n", detail="d", detail_msg=msg)
        assert _invariant_rows([v])[0]["detail_msg"] == msg.to_wire()
        issue = SimpleNamespace(
            config_key="k",
            entity_id="e",
            friendly_name="F",
            friendly_name_msg=msg,
            criticality="c",
            status="s",
            detail="d",
            detail_msg=msg,
        )
        row = _entity_health_rows([issue])[0]
        assert row["friendly_name_msg"] == msg.to_wire() and row["detail_msg"] == msg.to_wire()
