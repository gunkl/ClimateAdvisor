"""Issue #1341 lane L3: occupant notifications, banners' ``detail`` and promise text are catalog messages.

Every converted sink renders the exact pre-#1341 English in ``en``; ``_notify`` also renders in another language from
that language's catalog (a temporary Russian catalog here, because the real translations arrive in a later phase) with
every parameter substituted and no English left over. The ``Msg`` wire round-trip is pinned for the ``*_msg`` siblings.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import re
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.climate_advisor import i18n
from custom_components.climate_advisor.automation import AutomationEngine
from custom_components.climate_advisor.entity_health import (
    EntityHealthIssue,
    entity_health_consequence,
    entity_health_consequence_msg,
    run_entity_health_sweep,
)
from custom_components.climate_advisor.invariant_watchdog import check_ac_whf_mutex
from custom_components.climate_advisor.whf_safety_decision import (
    WhfSafetyAction,
    WhfSafetyInputs,
    decide_whf_safety,
)

_PKG = Path(i18n.__file__).parent
_NOTIFY_FILES = (
    "automation.py",
    "coordinator.py",
    "invariant_watchdog.py",
    "entity_health.py",
    "whf_safety_decision.py",
)


def en(msg: i18n.Msg) -> str:
    return i18n.render_en(msg)


class TestCatalog:
    def test_every_english_message_parses(self):
        catalog = json.loads((_PKG / "frontend" / "i18n" / "notify" / "en.json").read_text(encoding="utf-8"))
        assert catalog
        for key, pattern in catalog.items():
            assert key.startswith("notify."), key
            i18n.parse_message(pattern)

    def test_every_key_in_the_lane_sources_exists_and_every_key_is_used(self):
        catalog = json.loads((_PKG / "frontend" / "i18n" / "notify" / "en.json").read_text(encoding="utf-8"))
        used: set[str] = set()
        for name in _NOTIFY_FILES:
            text = (_PKG / name).read_text(encoding="utf-8")
            used |= set(re.findall(r'"(notify\.[a-z0-9_.]+)"', text))
        used.discard("notify.mobile_app_x")  # an example service name in a docstring, not a key
        used |= {k for k in catalog if k.startswith("notify.entity.")}  # keyed by config key in entity_health.py
        assert used - set(catalog) == set(), "a source file names a key the catalog lacks"
        assert set(catalog) - used == set(), "a catalog key nothing uses"


@pytest.fixture
def ru_catalog(tmp_path):
    """The real catalogs plus a Russian ``notify`` file holding a few real Russian messages."""
    root = tmp_path / "frontend"
    shutil.copytree(_PKG / "frontend" / "i18n", root / "i18n")
    ru = {
        "notify.door_window_paused_named": (
            "\U0001f6aa Отопление и охлаждение приостановлены — {name} открыто уже "
            "{minutes, plural, one {# минуту} few {# минуты} many {# минут} other {# минуты}}. "
            "Работа возобновится, когда оно будет закрыто."
        ),
        "notify.close_windows": (
            "В доме {indoor}, выше вашего предела комфорта {limit}, а окна открыты. Закройте окна, и дом остынет."
        ),
        "notify.grace_expired_manual": "Ваше ручное изменение термостата закончилось.",
        "notify.title.close_windows": "Climate Advisor — закройте окна",
    }
    (root / "i18n" / "notify" / "ru.json").write_text(json.dumps(ru, ensure_ascii=False), encoding="utf-8")
    i18n.load_catalogs(root)
    yield
    i18n.reset_catalogs()
    i18n.load_catalogs()


def _engine(language: str = "en", **config) -> AutomationEngine:
    hass = MagicMock()
    hass.config.language = language
    hass.services.async_call = AsyncMock()
    hass.async_create_task = MagicMock(side_effect=lambda coro: coro.close())
    cfg = {
        "comfort_heat": 70,
        "comfort_cool": 75,
        "setback_heat": 60,
        "setback_cool": 80,
        "notify_service": "notify.notify",
        "temp_unit": "fahrenheit",
    }
    cfg.update(config)
    return AutomationEngine(
        hass=hass,
        climate_entity="climate.thermostat",
        weather_entity="weather.forecast_home",
        door_window_sensors=["binary_sensor.front_door"],
        notify_service="notify.notify",
        config=cfg,
    )


def _sent(engine: AutomationEngine) -> list[dict]:
    return [c.args[2] for c in engine.hass.services.async_call.await_args_list]


class TestOtherLanguage:
    def test_named_door_notice_in_russian_substitutes_name_and_minutes(self, ru_catalog):
        out = i18n.render(i18n.Msg("notify.door_window_paused_named", name="Front Door", minutes=10), "ru")
        assert "Front Door" in out and "10 минут" in out
        assert "HVAC" not in out and "has been open" not in out

    def test_close_windows_in_russian_formats_the_temperature_for_the_language(self, ru_catalog):
        msg = i18n.Msg(
            "notify.close_windows", indoor=i18n.Temp(77.5, "celsius", 1), limit=i18n.Temp(75.0, "celsius", 1)
        )
        out = i18n.render(msg, "ru")
        assert "25,3°C" in out and "23,9°C" in out
        assert "Close the windows" not in out

    def test_notify_renders_a_msg_in_the_home_assistant_language(self, ru_catalog):
        engine = _engine(language="ru")
        asyncio.run(
            engine._notify(
                i18n.Msg("notify.grace_expired_manual"),
                i18n.Msg("notify.title.close_windows"),
                notification_type="grace_expired",
            )
        )
        assert _sent(engine)[0] == {
            "message": "Ваше ручное изменение термостата закончилось.",
            "title": "Climate Advisor — закройте окна",
        }


class TestNotifySink:
    def test_plain_strings_are_sent_unchanged(self):
        engine = _engine()
        asyncio.run(engine._notify("hello", "Climate Advisor", notification_type="grace_expired"))
        assert _sent(engine)[0] == {"message": "hello", "title": "Climate Advisor"}

    def test_a_msg_is_rendered_in_english_for_an_english_install(self):
        engine = _engine()
        asyncio.run(
            engine._notify(
                i18n.Msg("notify.grace_expired_automation", minutes=5),
                i18n.Msg("notify.title.close_windows"),
                notification_type="grace_expired",
                email=False,
            )
        )
        assert _sent(engine) == [
            {
                "message": "Automation grace period expired (5 minutes). "
                "HVAC will now respond normally to door/window sensor changes.",
                "title": "Climate Advisor — close the windows",
            }
        ]


class TestAutomationNotices:
    def test_named_door_window_pause(self):
        assert en(i18n.Msg("notify.door_window_paused_named", name="Front Door", minutes=10)) == (
            "\U0001f6aa HVAC paused — Front Door has been open for 10 minutes. "
            "Heating/cooling will resume when it's closed."
        )

    def test_generic_door_window_pause_and_grace_texts(self):
        assert en(i18n.Msg("notify.door_window_paused")) == (
            "\U0001f6aa HVAC paused — a monitored door/window is open. Heating/cooling will resume when it's closed."
        )
        assert en(i18n.Msg("notify.grace_repause")) == (
            "Grace period expired but a door/window is still open. HVAC has been paused again."
        )
        assert en(i18n.Msg("notify.grace_expired_manual")) == (
            "Your manual thermostat override has expired. Climate Advisor has resumed automated control."
        )
        assert en(i18n.Msg("notify.override_self_resolved")) == (
            "Brief thermostat adjustment detected — treated as transient (reverted within confirmation window). "
            "Climate Advisor continues normal operation."
        )

    @pytest.mark.parametrize(
        ("hold", "text"),
        [
            ("manual_override", "your manual thermostat change"),
            ("override_confirm_pending", "your pending thermostat change"),
            ("paused_by_door", "the open door/window"),
            ("open_sensor", "the open door/window"),
            ("other", "the hold"),
        ],
    )
    def test_welcome_home_hold_names_the_reason(self, hold, text):
        assert en(i18n.Msg("notify.welcome_home_hold", hold=hold)) == (
            f"\U0001f3e0 Welcome home! Comfort temperature will resume once {text} clears."
        )

    def test_close_windows_in_both_units(self):
        f = en(
            i18n.Msg(
                "notify.close_windows", indoor=i18n.Temp(78.4, "fahrenheit", 1), limit=i18n.Temp(75, "fahrenheit", 1)
            )
        )
        assert f.startswith("It's 78.4°F inside, above your 75.0°F comfort limit, and the windows are open.")
        c = en(i18n.Msg("notify.close_windows", indoor=i18n.Temp(77, "celsius", 1), limit=i18n.Temp(75, "celsius", 1)))
        assert c.startswith("It's 25.0°C inside, above your 23.9°C comfort limit")

    def test_whf_stale_sensor_notice_with_names_and_with_a_bare_count(self):
        assert en(i18n.Msg("notify.whf_stale_sensors", names="Patio", count=1)) == (
            "Patio has been unavailable for over a minute. Climate Advisor counts it as closed when deciding "
            "whether the whole-house fan may run."
        )
        many = en(i18n.Msg("notify.whf_stale_sensors", names=i18n.Msg("notify.sensor_count", n=3), count=3))
        assert many.startswith("3 sensors have been unavailable for over a minute.")

    def test_whf_stop_failed_details_and_oscillation(self):
        detail = i18n.Msg("notify.whf_stop_failed_detail_attempts", attempts=3)
        assert en(i18n.Msg("notify.whf_stop_failed", detail=detail)) == (
            "The whole-house fan is running with every door and window closed and Climate Advisor could not stop it: "
            "the fan was still running after 3 off commands. Turn it off at the remote or the wall switch, or open a "
            "window."
        )
        assert en(i18n.Msg("notify.whf_oscillation", count=3)).startswith(
            "The whole-house fan was turned on 3 times in 10 minutes"
        )

    def test_indoor_blind_alert_variants(self):
        when = datetime(2026, 11, 14, 2, 30, tzinfo=UTC)
        base = (
            "The indoor temperature sensor stopped reporting while a ventilation fan is running, so Climate Advisor "
            "can't tell whether the house is getting too cold. "
        )
        assert en(i18n.Msg("notify.blind_alert", outcome="will_stop", time=i18n.Time(when))) == (
            base + "Climate Advisor will turn the fan off at 2:30 AM if it doesn't come back."
        )
        assert en(i18n.Msg("notify.blind_alert", outcome="override", time=i18n.Time(when))).endswith(
            "Climate Advisor will NOT turn it off — you may want to."
        )
        assert en(i18n.Msg("notify.blind_alert", outcome="other", time=i18n.Time(when))).endswith(
            "Climate Advisor will not turn this fan off automatically — you may want to."
        )

    @pytest.mark.parametrize(
        ("when", "tail", "expected"),
        [
            ("short", "stopped", "reporting again after a short outage. The fan stays off; ventilation may resume"),
            ("at_least", "running", "reporting again after at least 30 minutes. Climate Advisor is monitoring"),
            ("about", "off", "reporting again after about 30 minutes. The fan is no longer running."),
        ],
    )
    def test_indoor_blind_recovery_variants(self, when, tail, expected):
        assert expected in en(i18n.Msg("notify.blind_recovered", when=when, tail=tail, minutes=30))

    def test_indoor_blind_stopped(self):
        assert en(i18n.Msg("notify.blind_stopped", minutes=45)) == (
            "The indoor temperature sensor has been unavailable for 45 minutes, so Climate Advisor turned the "
            "ventilation fan off to avoid overcooling the house. Please check the sensor."
        )

    def test_whf_safety_stopped_texts(self):
        assert en(i18n.Msg("notify.whf_stopped_sealed")) == (
            "Climate Advisor turned off the whole-house fan because every monitored door and window is closed. "
            "It will not restart the fan until a window opens."
        )
        assert en(i18n.Msg("notify.whf_stopped_rule2")).startswith(
            "Climate Advisor turned off the whole-house fan because the heating/cooling was turned on after it."
        )


class TestInvariantAndPromiseText:
    def test_ac_whf_mutex_detail_keeps_its_english_and_carries_the_msg(self):
        v = check_ac_whf_mutex(hvac_action="cooling", whf_physically_on=True, fan_mode="whole_house_fan")
        assert v is not None
        assert v.detail == (
            "AC is actively cooling while the whole-house fan is physically running — "
            "these must never run at the same time."
        )
        assert i18n.Msg.from_wire(v.detail_msg.to_wire()) == v.detail_msg
        assert en(v.detail_msg) == v.detail

    def test_p1_p4_p7_details_render_with_typed_temperatures(self):
        heat = en(i18n.Msg("notify.promise_p1_heat", since=12, near="off", dip=i18n.TempDelta(1.5, "fahrenheit", 1)))
        assert heat == (
            "Your heat started 12 min after the AC cooled the house, while the house was only 1.5°F under your "
            "comfort minimum. It should have waited."
        )
        cool = en(i18n.Msg("notify.promise_p1_cool", since=7, near="at", dip=i18n.TempDelta(0, "fahrenheit", 1)))
        assert "while the house was right at your comfort maximum" in cool
        assert en(i18n.Msg("notify.promise_p4_changed", target="fan", when="in_force", minutes=0)) == (
            "Climate Advisor changed your fan setting while your manual change was still in force."
        )
        assert en(i18n.Msg("notify.promise_p4_changed", target="thermostat", when="minutes_left", minutes=20)) == (
            "Climate Advisor changed your thermostat setting 20 min before your manual change was due to end."
        )
        assert en(i18n.Msg("notify.promise_p7_comfort_kept", worst=i18n.TempDelta(5, "celsius", 1), minutes=50)) == (
            "The house stayed 2.8°C outside your comfort range for 50 minutes while Climate Advisor was in control."
        )

    def test_other_promise_details(self):
        assert en(i18n.Msg("notify.promise_p9_whf_sealed", seconds=95)) == (
            "The whole-house fan ran 95 seconds with every door and window closed."
        )
        assert en(i18n.Msg("notify.promise_p10_settles", count=4, hours=3.0)) == (
            "Climate Advisor switched the thermostat between heat and cool 4 times in the past 3 hours."
        )
        assert en(i18n.Msg("notify.promise_p6_flip_rate", count=3)) == (
            "The thermostat flipped between heat and cool 3 times in the past hour."
        )


class TestWhfDecisionReasons:
    def _inputs(self, **kw):
        base = {
            "fan_mode": "whole_house_fan",
            "fan_on": True,
            "fan_believed_on": True,
            "fan_on_for_s": 120.0,
            "sensors": (),
            "sealed_confirmed": False,
            "thermostat_mode": "off",
            "occupant_fan_on_at": None,
            "occupant_hvac_on_at": None,
            "whf_owns_hvac": False,
        }
        base.update(kw)
        return WhfSafetyInputs(**base)

    def test_not_a_whf_install_and_fan_not_running(self):
        d = decide_whf_safety(self._inputs(fan_mode="disabled"))
        assert d.action is WhfSafetyAction.NONE
        assert d.reason == "not a whole-house-fan install"
        assert d.reason_msg == i18n.Msg("notify.whf_reason_not_a_whf")
        assert decide_whf_safety(self._inputs(fan_on=False, fan_believed_on=False)).reason == "fan not running"

    def test_stop_in_flight_reason_has_its_numbers(self):
        d = decide_whf_safety(self._inputs(stop_in_flight_for_s=7.0))
        assert d.reason.startswith("stop in flight (7s of ")
        assert i18n.Msg.from_wire(d.reason_msg.to_wire()) == d.reason_msg


def _issue(config_key="climate_entity", **kw):
    base = {
        "config_key": config_key,
        "entity_id": "climate.thermostat",
        "friendly_name": "Thermostat",
        "criticality": "critical",
        "status": "missing",
        "friendly_name_msg": i18n.Msg("notify.entity.climate_entity"),
    }
    base.update(kw)
    return EntityHealthIssue(**base)


class TestEntityHealthText:
    def test_the_sweep_names_issues_with_a_translatable_name_and_the_same_english(self):
        hass = MagicMock()
        hass.states.get.return_value = None
        hass.services.has_service.return_value = True
        issues = run_entity_health_sweep(
            hass, {"climate_entity": "climate.t", "door_window_sensors": ["binary_sensor.d"]}
        )
        by_key = {i.config_key: i for i in issues}
        assert by_key["climate_entity"].friendly_name == "Thermostat"
        assert by_key["climate_entity"].friendly_name_msg == i18n.Msg("notify.entity.climate_entity")
        assert by_key["door_window_sensors"].friendly_name == "Door/window sensor"
        assert i18n.Msg.from_wire(by_key["door_window_sensors"].friendly_name_msg.to_wire()).key == (
            "notify.entity.door_window_sensor"
        )

    def test_consequence_text_is_the_msg_in_english(self):
        door = _issue("door_window_sensors", entity_id="binary_sensor.d")
        for last, key in (
            ("open", "notify.entity_consequence_door_open"),
            ("closed", "notify.entity_consequence_door_closed"),
            (None, "notify.entity_consequence_door_unknown"),
        ):
            msg = entity_health_consequence_msg(door, door_last_reading=last)
            assert msg == i18n.Msg(key)
            assert entity_health_consequence(door, door_last_reading=last) == en(msg)
        assert entity_health_consequence(door, door_last_reading="open").startswith(
            "It last read open, so heating and cooling stay paused"
        )
        assert entity_health_consequence_msg(_issue(), whf_running=False) is None
        assert entity_health_consequence(_issue(), whf_running=False) == ""

    def test_single_and_multi_messages_in_english(self):
        single = en(
            i18n.Msg(
                "notify.entity_health_single",
                name=i18n.Msg("notify.entity.climate_entity"),
                entity_id="climate.thermostat",
                consequence=i18n.Msg("notify.entity_generic_consequence_single"),
            )
        )
        assert single == (
            "Thermostat ('climate.thermostat') isn't responding. Climate Advisor may not be able to control your HVAC "
            "correctly until it's fixed. Check Settings > Devices & Services."
        )
        multi = en(
            i18n.Msg(
                "notify.entity_health_multi",
                count=2,
                names=i18n.ListOf(
                    [
                        i18n.Msg("notify.entity_label", name="Thermostat", entity_id="climate.t"),
                        i18n.Msg("notify.entity_label", name="Weather source", entity_id="weather.h"),
                    ]
                ),
                details="X.",
            )
        )
        assert multi == (
            "2 entities aren't responding: Thermostat (climate.t) and Weather source (weather.h). X. "
            "Check Settings > Devices & Services."
        )


def _coord():
    mod = importlib.import_module("custom_components.climate_advisor.coordinator")
    coord = object.__new__(mod.ClimateAdvisorCoordinator)
    coord.hass = MagicMock()
    coord.hass.config.language = "en"
    coord.config = {"temp_unit": "fahrenheit"}
    return mod, coord


class TestCoordinatorShells:
    def test_notify_in_background_passes_msg_through_to_the_engine(self):
        mod, coord = _coord()
        engine = MagicMock()
        engine._notify = AsyncMock()
        tasks: list = []
        coord.hass.async_create_task = MagicMock(side_effect=tasks.append)
        coord.automation_engine = engine
        title, message = i18n.Msg("notify.title.stall_off_target"), i18n.Msg("notify.grace_repause")
        mod.ClimateAdvisorCoordinator._notify_in_background(coord, title, message, "hvac_output_stall", email=False)
        asyncio.run(tasks[0])
        engine._notify.assert_awaited_once_with(message, title, notification_type="hvac_output_stall", email=False)

    def test_entity_health_batch_builds_one_notification(self):
        mod, coord = _coord()
        sent: list = []
        coord._notify_in_background = lambda title, message, kind: sent.append((title, message, kind))
        issues = [
            _issue(detail="", detail_msg=None),
            _issue(
                "weather_entity",
                entity_id="weather.home",
                friendly_name="Weather source",
                friendly_name_msg=i18n.Msg("notify.entity.weather_entity"),
                status="unavailable",
                detail="Because.",
                detail_msg=i18n.Msg("notify.entity_consequence_door_open"),
            ),
        ]
        mod.ClimateAdvisorCoordinator._notify_entity_health_issues(coord, issues)
        title, message, kind = sent[0]
        assert kind == "entity_health"
        assert en(title) == "Climate Advisor — entities unavailable or not found"
        text = en(message)
        assert text.startswith("2 entities aren't responding: Thermostat (climate.thermostat) and Weather source")
        assert "Weather source (weather.home): It last read open" in text
        assert text.endswith("Check Settings > Devices & Services.")

    def test_control_issues_carry_english_detail_and_a_wire_msg(self):
        mod, coord = _coord()
        ae = MagicMock()
        ae.climate_entity = "climate.t"
        ae._manual_change_protected.return_value = False
        ae._hvac_write_record = MagicMock(
            episode_reason="heat", mismatch_since=datetime.now(UTC) - timedelta(minutes=12)
        )
        ae._hvac_cmd_fail_announced = None
        coord.automation_engine = ae
        coord._stall_status = None
        state = MagicMock()
        state.state = "off"
        coord.hass.states.get = lambda _e: state
        issues = mod.ClimateAdvisorCoordinator._compute_hvac_control_issues(coord)
        assert [i["issue"] for i in issues] == ["mode_not_obeyed"]
        assert issues[0]["detail"] == (
            "Thermostat is not following the requested mode: requested heat, reports 'off' for 12 min"
        )
        wire = issues[0]["detail_msg"]
        assert en(i18n.Msg.from_wire(wire)) == issues[0]["detail"]
        json.dumps(wire)

    def test_stall_texts_render_in_english(self):
        diag = i18n.Msg("notify.stall_diag", mode="cool", action="idle")
        msg = i18n.Msg(
            "notify.stall_message",
            gap=i18n.TempDelta(6.0, "fahrenheit", 1),
            direction="cool",
            edge=i18n.Temp(75, "fahrenheit"),
            hours=3,
            diag=diag,
        )
        assert en(msg) == (
            "The house is 6.0°F above your comfort band (75°F) and the cooling hasn't run for 3 hours "
            "(thermostat mode cool, action idle)."
        )
        status = i18n.Msg(
            "notify.control_output_stall",
            gap=i18n.TempDelta(6.0, "fahrenheit", 1),
            direction="heat",
            hours=1,
            diag=diag,
        )
        assert en(status) == (
            "House has been 6.0°F below the comfort band for 1 h with no heating running "
            "(thermostat mode cool, action idle)"
        )
