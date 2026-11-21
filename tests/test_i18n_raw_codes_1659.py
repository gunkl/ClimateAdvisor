"""Issue #1659: no notification or status line puts a raw HA state code into a translated sentence.

What the occupant saw: in any language but English, a safety notification read e.g. "空調正在 cooling" (zh-Hant)
or "Die Klimaanlage ist aktiv (cooling)" (de), because the message got the raw ``hvac_action`` / ``hvac_mode``
code as a plain text argument. Each such argument is now the code's word from the viewer's catalog
(``common.hvac_action.*``, ``common.hvac_mode_word.*``, ``status.pre_cool.outcome.*``,
``status.indoor_source.why.*``). English keeps exactly the text it had (its catalog word is the code itself).

Every test here renders through the real production builders (the invariant check, the WHF safety decision, the
coordinator's control-issue / stall / indoor-source builders, the real ``handle_pre_cool``), in every non-English
language, and fails if the raw code appears in the text instead of that language's word.
"""

from __future__ import annotations

import asyncio
import datetime
import importlib
import json
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.climate_advisor import i18n
from custom_components.climate_advisor.automation import HvacCommandResult
from custom_components.climate_advisor.hvac_action import (
    HVAC_ACTION_WORD_CODES,
    HVAC_MODE_WORD_CODES,
    hvac_action_word,
    hvac_mode_word,
)
from custom_components.climate_advisor.invariant_watchdog import check_ac_whf_mutex
from custom_components.climate_advisor.whf_safety_decision import (
    WHF_ACTIVE_HVAC_MODES,
    SensorReading,
    WhfSafetyAction,
    WhfSafetyInputs,
    decide_whf_safety,
)

_I18N = Path(i18n.__file__).parent / "frontend" / "i18n"
LANGS = json.loads((_I18N / "index.json").read_text(encoding="utf-8"))["languages"]
OTHER = [lang for lang in LANGS if lang != "en"]
OPEN = SensorReading(is_open=True, stable_for_s=3600.0)


def _catalog(area: str, lang: str) -> dict[str, str]:
    return json.loads((_I18N / area / f"{lang}.json").read_text(encoding="utf-8"))


def _assert_word_not_code(text: str, lang: str, key: str, code: str) -> None:
    """``text`` shows ``lang``'s word for ``code`` (``key``), and not the raw code where the two differ."""
    word = i18n.render(i18n.Msg(key), lang)
    assert word and word != key, (lang, key)
    assert word in text, (lang, code, word, text)
    if word.casefold() != code.replace("_", " ").casefold() and word.casefold() != code.casefold():
        assert not re.search(rf"(?<![A-Za-z_]){re.escape(code)}(?![A-Za-z_])", text), (lang, code, text)


# Compressor-active actions are the only ones the invariant and the rule-2 backstop ever report.
COMPRESSOR = ("heating", "cooling")


class TestCatalogWords:
    @pytest.mark.parametrize("lang", LANGS)
    def test_every_code_has_a_word_in_every_language(self, lang):
        common = _catalog("common", lang)
        for code in HVAC_ACTION_WORD_CODES:
            assert common.get(f"common.hvac_action.{code}"), (lang, code)
        for code in HVAC_MODE_WORD_CODES:
            assert common.get(f"common.hvac_mode_word.{code}"), (lang, code)

    def test_english_words_are_the_codes_so_english_text_is_unchanged(self):
        common = _catalog("common", "en")
        for code in HVAC_ACTION_WORD_CODES:
            assert common[f"common.hvac_action.{code}"] == code
        for code in HVAC_MODE_WORD_CODES:
            assert common[f"common.hvac_mode_word.{code}"] == code

    def test_unknown_and_missing_codes_never_crash(self):
        assert hvac_action_word("vendor_special") == "vendor_special"
        assert hvac_mode_word("eco") == "eco"
        for lang in LANGS:
            assert i18n.render(i18n.Msg("notify.stall_diag", mode=hvac_mode_word(None), action="x"), lang)
        assert i18n.render_en(
            i18n.Msg("notify.stall_diag", mode=hvac_mode_word(None), action=hvac_action_word(None))
        ) == ("thermostat mode unknown, action unknown")
        # Case-insensitive like the predicates (a thermostat reporting "Cooling").
        assert hvac_action_word("Cooling") == i18n.Code("common.hvac_action.cooling")


class TestInvariantNotification:
    @pytest.mark.parametrize("lang", OTHER)
    @pytest.mark.parametrize("action", COMPRESSOR)
    def test_translated_action_word(self, lang, action):
        v = check_ac_whf_mutex(hvac_action=action, whf_physically_on=True, fan_mode="whole_house_fan")
        assert v is not None
        text = i18n.render(v.detail_msg, lang)
        _assert_word_not_code(text, lang, f"common.hvac_action.{action}", action)

    @pytest.mark.parametrize("action", COMPRESSOR)
    def test_english_unchanged(self, action):
        v = check_ac_whf_mutex(hvac_action=action, whf_physically_on=True, fan_mode="whole_house_fan")
        assert v.detail == (
            f"AC is actively {action} while the whole-house fan is physically running — "
            "these must never run at the same time."
        )
        assert i18n.render_en(v.detail_msg) == v.detail


def _inputs(**kw) -> WhfSafetyInputs:
    base = {
        "fan_mode": "whole_house_fan",
        "fan_on": True,
        "fan_believed_on": False,
        "fan_on_for_s": 120.0,
        "sensors": (OPEN,),
        "sealed_confirmed": False,
        "thermostat_mode": "heat",
        "occupant_fan_on_at": None,
        "occupant_hvac_on_at": None,
        "whf_owns_hvac": True,
        "hvac_action": "heating",
        "overlap_for_s": 12.0,
        "thermostat_mode_for_s": 30.0,
    }
    base.update(kw)
    return WhfSafetyInputs(**base)


T0 = datetime.datetime(2026, 10, 30, 21, 0, tzinfo=datetime.UTC)
_BACKSTOP = {
    # name: (inputs, expected action, English pattern with {mode}/{action})
    "stop_fan": (
        {"thermostat_mode_for_s": 30.0},
        WhfSafetyAction.STOP_MUTEX_FAN,
        "rule-2 state backstop: thermostat switched to {mode} after the fan came on and is {action} beside it (12s)"
        " — stopping the fan (newest action wins)",
    ),
    "suppress_hvac": (
        {"thermostat_mode_for_s": 600.0},
        WhfSafetyAction.SUPPRESS_HVAC,
        "rule-2 state backstop: thermostat {action} ({mode}) beside the running fan (12s) — suppressing "
        "heating/cooling (the fan is the newer action)",
    ),
    "occupant_hvac_after_fan": (
        {"occupant_hvac_on_at": T0, "occupant_fan_on_at": None},
        WhfSafetyAction.STOP_MUTEX_FAN,
        "occupant turned HVAC on ({mode}) after the fan — newest action wins",
    ),
    "occupant_fan_after_hvac": (
        {"occupant_fan_on_at": T0, "whf_owns_hvac": False},
        WhfSafetyAction.SUPPRESS_HVAC,
        "occupant turned the fan on after HVAC ({mode}) — newest action wins",
    ),
}
_MODE_ACTION = [(m, a) for m in sorted(WHF_ACTIVE_HVAC_MODES) for a in COMPRESSOR]


class TestWhfSafetyReasons:
    @pytest.mark.parametrize("name", sorted(_BACKSTOP))
    @pytest.mark.parametrize(("mode", "action"), _MODE_ACTION)
    def test_every_language_shows_words_not_codes(self, name, mode, action):
        kw, expected, _ = _BACKSTOP[name]
        d = decide_whf_safety(_inputs(thermostat_mode=mode, hvac_action=action, **kw))
        assert d.action is expected and d.reason_msg is not None
        for lang in OTHER:
            text = i18n.render(d.reason_msg, lang)
            _assert_word_not_code(text, lang, f"common.hvac_mode_word.{mode}", mode)
            if "{action}" in _BACKSTOP[name][2]:
                _assert_word_not_code(text, lang, f"common.hvac_action.{action}", action)

    @pytest.mark.parametrize("name", sorted(_BACKSTOP))
    @pytest.mark.parametrize(("mode", "action"), _MODE_ACTION)
    def test_english_unchanged(self, name, mode, action):
        kw, _, pattern = _BACKSTOP[name]
        d = decide_whf_safety(_inputs(thermostat_mode=mode, hvac_action=action, **kw))
        assert d.reason == pattern.format(mode=mode, action=action)
        assert i18n.render_en(d.reason_msg) == d.reason


def _coord():
    mod = importlib.import_module("custom_components.climate_advisor.coordinator")
    coord = object.__new__(mod.ClimateAdvisorCoordinator)
    coord.hass = MagicMock()
    coord.hass.config.language = "en"
    coord.config = {"temp_unit": "fahrenheit"}
    return mod, coord


def _control_issues(requested: str, live: str | None, *, failed: str | None = None):
    mod, coord = _coord()
    ae = MagicMock()
    ae.climate_entity = "climate.t"
    ae._manual_change_protected.return_value = False
    now = datetime.datetime.now(datetime.UTC)
    ae._hvac_write_record = MagicMock(episode_reason=requested, mismatch_since=now - datetime.timedelta(minutes=12))
    ae._hvac_cmd_fail_announced = bool(failed)
    ae._hvac_cmd_fail_mode = failed
    ae._hvac_cmd_fail_since = now - datetime.timedelta(minutes=30)
    coord.automation_engine = ae
    coord._stall_status = None
    coord.hass.states.get = lambda _e: SimpleNamespace(state=live)
    return mod.ClimateAdvisorCoordinator._compute_hvac_control_issues(coord)


_LIVE = ["off", "heat", "cool", "heat_cool", "auto", "dry", "fan_only", "unavailable", "unknown"]


class TestControlIssues:
    @pytest.mark.parametrize("live", _LIVE)
    def test_mode_not_obeyed_in_every_language(self, live):
        requested = "cool" if live != "cool" else "heat"
        (issue,) = _control_issues(requested, live)
        assert issue["issue"] == "mode_not_obeyed"
        msg = i18n.Msg.from_wire(issue["detail_msg"])
        for lang in OTHER:
            text = i18n.render(msg, lang)
            _assert_word_not_code(text, lang, f"common.hvac_mode_word.{requested}", requested)
            _assert_word_not_code(text, lang, f"common.hvac_mode_word.{live}", live)

    def test_mode_not_obeyed_english_unchanged(self):
        (issue,) = _control_issues("heat", "off")
        assert (
            issue["detail"]
            == "Thermostat is not following the requested mode: requested heat, reports 'off' for 12 min"
        )
        assert i18n.render_en(i18n.Msg.from_wire(issue["detail_msg"])) == issue["detail"]
        (issue,) = _control_issues("heat_cool", "fan_only")
        assert issue["detail"] == (
            "Thermostat is not following the requested mode: requested heat_cool, reports 'fan_only' for 12 min"
        )

    @pytest.mark.parametrize("failed", ["heat", "cool", "heat_cool", "off", "auto", "fan_only"])
    def test_command_failing_in_every_language(self, failed):
        # live equals the requested mode, so only the command-failing entry shows.
        issues = _control_issues("dry", "dry", failed=failed)
        (issue,) = [i for i in issues if i["issue"] == "command_failing"]
        msg = i18n.Msg.from_wire(issue["detail_msg"])
        assert i18n.render_en(msg) == (f"Thermostat commands are failing (last attempted mode: {failed}) for 30 min")
        for lang in OTHER:
            _assert_word_not_code(i18n.render(msg, lang), lang, f"common.hvac_mode_word.{failed}", failed)


class TestStallDiag:
    @pytest.mark.parametrize("mode", ["heat", "cool", "heat_cool", "off", "unknown"])
    @pytest.mark.parametrize("action", ["idle", "off", "fan", "heating", "cooling", "drying"])
    def test_stall_diag_message_words(self, mode, action):
        # The coordinator builds the diag from the live state with these helpers (coordinator.py, stall alert).
        msg = i18n.Msg("notify.stall_diag", mode=hvac_mode_word(mode), action=hvac_action_word(action))
        assert i18n.render_en(msg) == f"thermostat mode {mode}, action {action}"
        for lang in OTHER:
            text = i18n.render(msg, lang)
            _assert_word_not_code(text, lang, f"common.hvac_mode_word.{mode}", mode)
            _assert_word_not_code(text, lang, f"common.hvac_action.{action}", action)

    def test_coordinator_uses_the_word_helpers_for_the_stall_diag(self):
        src = (Path(i18n.__file__).parent / "coordinator.py").read_text(encoding="utf-8")
        assert 'i18n.Msg("notify.stall_diag", mode=hvac_mode_word(mode_txt), action=hvac_action_word(action))' in src


class TestIndoorSourceSleepReason:
    @pytest.mark.parametrize(
        "reason",
        [
            "entity_missing",
            "implausible",
            "no_reading",
            "non_finite",
            "non_numeric",
            "not_configured",
            "stale",
            "unavailable",
        ],
    )
    def test_every_language(self, reason):
        mod, coord = _coord()
        coord._indoor_source_left_sleep_failed = False
        reading = SimpleNamespace(remote_context=None, sleep_reason=reason)
        msg = coord._indoor_source_reason(reading, "climate.t", "sleep_sensor")
        assert i18n.render_en(msg) == f"sleep sensor not usable ({reason.replace('_', ' ')})"
        for lang in OTHER:
            text = i18n.render(msg, lang)
            word = i18n.render(i18n.Msg(f"status.indoor_source.why.{reason}"), lang)
            assert word in text and reason.replace("_", " ") not in text and reason not in text, (lang, text)

    def test_an_unlisted_reason_still_renders(self):
        mod, coord = _coord()
        coord._indoor_source_left_sleep_failed = False
        msg = coord._indoor_source_reason(
            SimpleNamespace(remote_context=None, sleep_reason="new_code"), "c", "sleep_sensor"
        )
        assert i18n.render_en(msg) == "sleep sensor not usable (new code)"
        assert "new code" in i18n.render(msg, "de")


class TestPreCoolNotApplied:
    @pytest.mark.parametrize(
        ("result", "outcome"),
        [
            (HvacCommandResult.WHF_BLOCKED, "blocked"),
            (HvacCommandResult.DRY_RUN, "dry_run"),
            (HvacCommandResult.FAILED, "failed"),
            (HvacCommandResult.SUPERSEDED, "superseded"),
            (None, "not_sent"),
        ],
    )
    def test_outcome_word_in_every_language(self, result, outcome):
        from tests.test_armed_band_surfaces_1136_1120 import _CFG, _cls
        from tests.test_mode_change_clock import _make_engine

        engine = _make_engine(indoor=75.0, mode="off")
        engine.config.update(_CFG)
        c = _cls()
        c.tomorrow_high = 100
        c.setback_modifier = -3.0
        engine._current_classification = c
        engine._apply_comfort_band = AsyncMock(return_value=result)
        now = datetime.datetime(2026, 10, 28, 3, 0, tzinfo=datetime.UTC)
        with patch("custom_components.climate_advisor.automation.dt_util.now", return_value=now):
            coded = asyncio.run(engine.handle_pre_cool(75.0, False))
        assert coded.code == "not_applied"
        assert str(coded).startswith(f"not applied ({outcome}): ")  # English unchanged
        for lang in OTHER:
            text = i18n.render(coded.msg, lang)
            word = i18n.render(i18n.Msg(f"status.pre_cool.outcome.{outcome}"), lang)
            assert word in text and outcome not in text, (lang, text)
