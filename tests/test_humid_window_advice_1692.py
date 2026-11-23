"""Issues #1692 / #1696 (plan #1507 H1 window advice, H5 briefing): humid outdoor air withdraws the window advice.

Occupant view: on a muggy night (outdoor dew point at or above the 65 °F ceiling, or humid air wetter than the house)
Climate Advisor no longer says "open the windows": the windows gate clears the day's window advice (which also ends
the planned window period), the Next Action card and the plan's open step say "keep windows closed, it's humid", the
night strategy drops its open-window options, and with a window already open the occupant is told ONCE to close it
("the AC will take over"). The briefing and the AI context show the indoor and outdoor dew point and name what the
guard changed. A dry night (and the guard switched Off, and unknown humidity) is today's behaviour.

Each test fails if the feature it names is deleted. Simulation only (FakeHassSession = real coordinator + engine).
"""

from __future__ import annotations

import importlib
import math
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from custom_components.climate_advisor import i18n
from custom_components.climate_advisor import night_strategy as ns
from custom_components.climate_advisor.briefing import BriefingHumidity, generate_briefing
from custom_components.climate_advisor.classifier import DayClassification
from custom_components.climate_advisor.const import CONF_HUMID_AIR_GUARD_ENABLED
from custom_components.climate_advisor.engine_inputs import EngineFacts
from custom_components.climate_advisor.feels_like import dew_point_f
from custom_components.climate_advisor.operation_timeline import SENTENCES
from custom_components.climate_advisor.timeline_surfaces import (
    CardLive,
    advises_opening,
    apply_direction_veto,
    timeline_windows,
    window_card_msg,
)
from tests.test_night_strategy import warm_night
from tools.sim_harness.run_production import FakeHassSession

WINDOW = "binary_sensor.living_room_window"
DAY = "2026-07-15"


def _t(hhmm: str) -> str:
    return f"{DAY}T{hhmm}:00"


def _at(hhmm: str) -> datetime:
    return datetime.fromisoformat(_t(hhmm)).replace(tzinfo=UTC)


# Outdoor 70 °F at 90 % RH = dew point 66.9 °F (>= the 65 °F ceiling); 62 °F at 50 % = 43.1 °F (dry).
HUMID = {"outdoor_f": 70.0, "outdoor_humidity": 90.0}
DRY = {"outdoor_f": 62.0, "outdoor_humidity": 50.0}
INDOOR = {"indoor_f": 75.0, "humidity": 50.0}  # house dew point 55.0 °F


def _config(**over: Any) -> dict[str, Any]:
    return {
        "comfort_heat": 68,
        "comfort_cool": 74,
        "setback_heat": 60,
        "setback_cool": 80,
        "sleep_time": "22:30",
        "wake_time": "06:30",
        "initial_thermostat_mode": "cool",
        "outdoor_temp_source": "sensor",
        "outdoor_temp_entity": "sensor.outdoor_temp",
        "door_window_sensors": [WINDOW],
        **over,
    }


def _session(first: dict | None = None, **config: Any) -> FakeHassSession:
    scenario = {
        "name": "humid_window_advice_1692",
        "use_coordinator": True,
        "skip_startup_coalesce": True,
        "config": _config(**config),
        "events": [{"time": _t("17:00"), "type": "temp_update", **(first or {**INDOOR, **DRY})}],
    }
    session = FakeHassSession(scenario, use_coordinator=True)
    session.feed(scenario["events"][0])
    return session


def _go(session: FakeHassSession, hhmm: str, event: dict, settle_s: int = 30) -> None:
    session.advance_to(_at(hhmm))
    session.feed({"time": _t(hhmm), **event})
    session.advance_to(_at(hhmm) + timedelta(seconds=settle_s))


def _window_day(session: FakeHassSession, hhmm: str) -> None:
    """A warm day with an evening window period (17:00-22:00), as the classifier would plan it."""
    _go(
        session,
        hhmm,
        {
            "type": "classification",
            "day_type": "warm",
            "hvac_mode": "off",
            "windows_recommended": True,
            "window_open_time": "17:00",
            "window_close_time": "22:00",
        },
    )


def _air(session: FakeHassSession, hhmm: str, outdoor: dict, indoor: dict | None = None, *, hold_min: int = 0) -> None:
    """Feed one reading; ``hold_min`` keeps the clock running that long after it (the decision dew point is the
    15-minute median of the 5-minute samples, so a new air mass takes up to ~15 minutes to become the fact)."""
    _go(session, hhmm, {"type": "temp_update", **(indoor or INDOOR), **outdoor})
    if hold_min:
        session.advance_to(_at(hhmm) + timedelta(minutes=hold_min))


def _notices(session: FakeHassSession) -> list[dict]:
    return [e for e in session.coordinator._event_log if e.get("type") == "humid_windows_close_notice"]


def _pushes(session: FakeHassSession) -> list[dict]:
    return [
        a
        for a in session.fake_hass.action_log
        if a.get("domain") == "notify" and "dew point" in str((a.get("data") or {}).get("message", "")).lower()
    ]


def _card(session: FakeHassSession, *, open_: bool) -> Any:
    c = session.coordinator
    return c._compute_next_action(
        c._current_classification,
        indoor_temp=c._get_indoor_temp(),
        outdoor_temp=c._last_outdoor_temp,
        windows_physically_open=open_,
        ae=c.automation_engine,
    )


# ── the windows gate: the advice and the planned window period (coordinator mode) ─────────────────────────────────


class TestPlannedWindowPeriod:
    def test_humid_withdrawal_ends_the_period_and_a_dry_cycle_restores_it(self):
        """H1 extra: a humid reading clears windows_recommended (ending the planned period); once the latch releases,
        the next classification cycle's window advice survives the gate again."""
        with _session() as session:
            _window_day(session, "17:05")
            _air(session, "17:06", DRY)
            c, ae = session.coordinator, session.engine
            assert c._current_classification.windows_recommended is True
            assert ae._is_within_planned_window_period() is True

            _air(session, "17:20", HUMID, hold_min=16)
            assert c._current_classification.windows_recommended is False
            assert ae._is_within_planned_window_period() is False
            assert c._humid_air_verdict().reason == "ceiling"

            # the latch releases on dry air; the next cycle's classification restores the advice through the gate
            _air(session, "18:00", DRY, hold_min=20)
            _window_day(session, "18:21")
            _air(session, "18:22", DRY)
            assert c._humid_air_verdict().blocks is False
            assert c._current_classification.windows_recommended is True
            assert ae._is_within_planned_window_period() is True

    def test_dry_control_keeps_the_advice(self):
        with _session() as session:
            _window_day(session, "17:05")
            _air(session, "17:20", DRY, hold_min=16)
            assert session.coordinator._current_classification.windows_recommended is True
            assert session.coordinator._humid_air_verdict().blocks is False

    def test_guard_off_keeps_the_advice_on_a_humid_night(self):
        with _session(**{CONF_HUMID_AIR_GUARD_ENABLED: False}) as session:
            _window_day(session, "17:05")
            _air(session, "17:20", HUMID, hold_min=16)
            assert session.coordinator._current_classification.windows_recommended is True

    def test_install_without_any_humidity_is_todays_behaviour(self):
        """No humidity anywhere (no sensor, no weather humidity): dew points unknown, the advice stands."""
        with _session(first={"indoor_f": 75.0, "outdoor_f": 70.0}) as session:
            _window_day(session, "17:05")
            _go(session, "17:20", {"type": "temp_update", "indoor_f": 75.0, "outdoor_f": 70.0})
            session.advance_to(_at("17:40"))
            facts = session.coordinator.engine_facts(session.now())
            assert facts.outdoor_dew_point_f is None and facts.indoor_dew_point_f is None
            assert session.coordinator._current_classification.windows_recommended is True
            assert session.coordinator._humid_blocks() is None


class TestInstalls:
    """The plan's install matrix for the advice: two zones (one without humidity), a WHF install, windows only."""

    @staticmethod
    def _zone(outdoor: dict, indoor: dict) -> tuple[bool, Any]:
        """One zone's coordinator through a window evening: (advice still standing, verdict)."""
        first = {**indoor, **DRY}
        with _session(first=first) as zone:
            _window_day(zone, "17:05")
            _air(zone, "17:20", outdoor, indoor=indoor, hold_min=16)
            return zone.coordinator._current_classification.windows_recommended, zone.coordinator._humid_air_verdict()

    def test_two_zones_one_without_indoor_humidity(self):
        """Two zones share the outdoor sensor; zone B has no indoor humidity. Each zone is its own coordinator, as in
        production (the guard is per zone); the harness patches one clock per session, so they run one after another.
        """
        zone_b_indoor = {"indoor_f": 75.0}  # no humidity: the indoor dew point is unknown
        # muggy (above the ceiling): the ceiling clause needs only the outdoor dew point, both zones withdraw
        for indoor in (INDOOR, zone_b_indoor):
            standing, verdict = self._zone(HUMID, indoor)
            assert standing is False and verdict.reason == "ceiling"
        # humid but under the ceiling and wetter than zone A's house (59.8 vs 55 F): only zone A, which knows its
        # indoor dew point, withdraws; zone B cannot judge "wetter than the house" (today's behaviour)
        wetter = {"outdoor_f": 70.0, "outdoor_humidity": 70.0}
        standing_a, verdict_a = self._zone(wetter, INDOOR)
        assert standing_a is False and verdict_a.reason == "wetter_than_house"
        standing_b, verdict_b = self._zone(wetter, zone_b_indoor)
        assert standing_b is True and verdict_b.blocks is False and verdict_b.indoor_dp_f is None

    def test_whole_house_fan_install(self):
        with _session(fan_mode="whole_house_fan", fan_entity="fan.whf") as session:
            _window_day(session, "17:05")
            _air(session, "17:20", HUMID, hold_min=16)
            assert session.coordinator._current_classification.windows_recommended is False
            assert not advises_opening(_card(session, open_=False))
            assert not session.engine._natural_vent_active  # windows shut: nothing started

    def test_windows_only_install(self):
        """No fan configured (the default ``_config``): the advice is the only lever and it is withdrawn."""
        with _session() as session:
            assert session.coordinator.config.get("fan_mode", "disabled") in ("disabled", "", None)
            _window_day(session, "17:05")
            _air(session, "17:20", HUMID, hold_min=16)
            assert session.coordinator._current_classification.windows_recommended is False


class TestNextActionCard:
    def test_humid_evening_never_invites_opening(self):
        with _session() as session:
            _go(
                session,
                "17:05",
                {
                    "type": "classification",
                    "day_type": "warm",
                    "hvac_mode": "off",
                    "windows_recommended": True,
                    "window_open_time": "19:00",
                    "window_close_time": "22:00",
                },
            )
            _air(session, "17:06", DRY)
            dry = _card(session, open_=False)
            assert advises_opening(dry) and dry.code == "open_windows_at"  # control: "Open windows at 7:00 PM"
            _air(session, "17:20", HUMID, hold_min=16)
            card = _card(session, open_=False)
            assert not advises_opening(card)
            assert card.code == "keep_closed_humid"
            assert str(card) == (
                "Keep windows closed for now — it's humid outside (dew point 67°F). "
                "I'll update the plan if it dries out."
            )
            # the guard releases: the card invites opening again (the plan's 7 PM step)
            _air(session, "18:00", DRY, hold_min=20)
            assert advises_opening(_card(session, open_=False))

    def test_open_window_is_told_to_close(self):
        with _session() as session:
            _window_day(session, "17:05")
            _air(session, "17:20", HUMID, hold_min=16)
            card = _card(session, open_=True)
            assert card.code == "close_windows_humid"
            assert str(card).startswith("Close the windows")


# ── windows OPEN on a humid night: paused, no ventilation, one notice ──────────────────────────────────────────────


class TestCloseWindowsNotice:
    def test_open_window_on_humid_night_pauses_and_notifies_once(self):
        with _session() as session:
            _window_day(session, "17:05")
            _air(session, "17:06", DRY)
            _go(session, "17:10", {"type": "sensor_open", "entity": WINDOW})
            assert _notices(session) == []  # a dry window evening: no notice
            _air(session, "17:20", HUMID, hold_min=16)
            ae = session.engine
            assert ae.is_paused_by_door is True  # the planned period ended: the open window pauses HVAC (rule 3)
            assert ae._natural_vent_active is False
            assert not ae._fan_active
            notices = _notices(session)
            assert len(notices) == 1
            assert notices[0]["reason"] == "ceiling"
            assert notices[0]["outdoor_dew_point"] == pytest.approx(dew_point_f(70.0, 90.0), abs=0.05)
            pushes = _pushes(session)
            assert len(pushes) == 1
            assert pushes[0]["data"]["message"] == (
                "Humid outside (dew point 67°F): close the windows and the AC will take over."
            )
            session.advance_to(_at("18:15"))  # many more ticks, same episode
            assert len(_notices(session)) == 1
            from custom_components.climate_advisor.api import humid_air_view

            line = humid_air_view(session.engine, "fahrenheit")  # the one humid-air Status line (#1691's field)
            assert line["code"] == "ceiling" and line["fan"] is False
            assert (
                line["text"]
                == "No open-window advice: humid outside (dew point 66.9°F, at or above the ceiling 65.0°F)"
            )

    def test_a_reopened_window_waits_out_the_hour_then_gets_one_reminder(self):
        """A new episode inside the 60-minute interval is held back; if the window is still open when the interval
        runs out it gets one notice then (one reminder an hour later), never one per tick."""
        with _session() as session:
            _air(session, "17:01", HUMID, hold_min=16)
            _go(session, "17:20", {"type": "sensor_open", "entity": WINDOW})
            session.advance_to(_at("17:26"))
            first = _notices(session)
            assert len(first) == 1
            first_at = datetime.fromisoformat(first[0]["time"])
            _go(session, "17:30", {"type": "sensor_close", "entity": WINDOW})
            session.advance_to(_at("17:36"))
            _go(session, "17:40", {"type": "sensor_open", "entity": WINDOW})
            session.advance_to(_at("18:15"))
            assert len(_notices(session)) == 1  # the new episode waits for the interval
            _air(session, "18:16", HUMID)  # keep the reading fresh
            session.advance_to(_at("18:45"))
            notices = _notices(session)
            assert len(notices) == 2
            delay = datetime.fromisoformat(notices[1]["time"]) - first_at
            assert timedelta(minutes=60) <= delay <= timedelta(minutes=65)  # sent on the first tick after the hour
            session.advance_to(_at("19:30"))
            assert len(_notices(session)) == 2  # still the same (second) episode

    def test_notice_while_cas_own_fan_session_runs(self):
        """D4: a running nat-vent session is kept when the guard engages; the notice still goes out (humid air is the
        reason to close) and stands for the blower-style close advice of that session."""
        warm = {"indoor_f": 77.0, "humidity": 50.0}
        with _session(fan_mode="whole_house_fan", fan_entity="fan.whf") as session:
            _window_day(session, "17:05")
            _air(session, "17:06", DRY, indoor=warm)
            _go(session, "17:10", {"type": "sensor_open", "entity": WINDOW})
            session.advance_to(_at("17:20"))
            ae = session.engine
            assert ae._natural_vent_active is True  # control: dry air, the fan session started
            _air(session, "17:21", HUMID, indoor=warm, hold_min=20)
            assert ae._natural_vent_active is True  # start-only: the running session is not ended here
            notices = _notices(session)
            assert len(notices) == 1 and notices[0]["ac_takes_over"] is True
            assert ae._close_windows_advised is True  # the session's own close advice will not push again
            assert _pushes(session)[0]["data"]["message"].endswith("the AC will take over.")

    def test_blower_session_says_the_ac_will_take_over(self):
        """N1: a blower (hvac_fan) session holds the thermostat Off with no recorded prior mode; on a warm day the band
        the engine arms after the windows close cools (ceiling active), so the push says the AC will take over.
        (The blower archetype starts only inside the band, #392: indoor 73.5 F under the 74 F ceiling.)"""
        warm = {"indoor_f": 73.5, "humidity": 50.0}
        with _session(fan_mode="hvac_fan") as session:
            _window_day(session, "17:05")
            _air(session, "17:06", DRY, indoor=warm)
            _go(session, "17:10", {"type": "sensor_open", "entity": WINDOW})
            session.advance_to(_at("17:20"))
            ae = session.engine
            assert ae._natural_vent_active is True  # control: the blower session started on dry air
            assert session.coordinator._thermostat_hvac_mode() == "off"
            _air(session, "17:21", HUMID, indoor=warm, hold_min=20)
            notices = _notices(session)
            assert len(notices) == 1 and notices[0]["ac_takes_over"] is True
            assert _pushes(session)[0]["data"]["message"].endswith("the AC will take over.")

    def test_dry_night_with_window_open_sends_nothing(self):
        with _session() as session:
            _air(session, "17:06", DRY)
            _go(session, "17:10", {"type": "sensor_open", "entity": WINDOW})
            session.advance_to(_at("17:41"))
            assert _notices(session) == [] and _pushes(session) == []

    def test_not_sent_while_away(self):
        with _session() as session:
            _go(session, "17:01", {"type": "occupancy_away"})
            _air(session, "17:06", HUMID, hold_min=16)
            _go(session, "17:30", {"type": "sensor_open", "entity": WINDOW})
            session.advance_to(_at("17:41"))
            assert session.coordinator._humid_air_verdict().blocks is True  # humid, open: only away holds it back
            assert _notices(session) == []

    def test_celsius_twin_says_the_dew_point_in_celsius(self):
        # The harness reports the thermostat in the install's unit (23.9 °C = 75 °F) and takes outdoor_f as internal °F
        # (it writes the outdoor sensor in °C itself): the same air as the °F twin, dew point 66.9 °F = 19.4 °C.
        indoor_c = {"indoor_f": 23.9, "humidity": 50.0}
        with _session(first={**indoor_c, **DRY}, temp_unit="celsius") as session:
            _go(session, "17:06", {"type": "temp_update", **indoor_c, **HUMID})
            session.advance_to(_at("17:22"))
            _go(session, "17:25", {"type": "sensor_open", "entity": WINDOW})
            session.advance_to(_at("17:36"))
            notices = _notices(session)
            assert len(notices) == 1
            assert notices[0]["outdoor_dew_point"] == pytest.approx(66.9, abs=0.2)  # stored internal °F, as F twin
            pushes = _pushes(session)
            assert pushes and "dew point 19°C" in pushes[0]["data"]["message"]


# ── the plan's open step and the briefing's window facts (pure) ───────────────────────────────────────────────────


def _live(**over: Any) -> CardLive:
    base: dict[str, Any] = {
        "unit": "fahrenheit",
        "indoor": 76.0,
        "outdoor": 70.0,
        "windows_open": False,
        "direction_ok": True,
        "heat_side_veto": False,
        "armed_heat": 68.0,
        "armed_cool": 74.0,
    }
    base.update(over)
    return CardLive(**base)


def _open_step_timeline(now: datetime) -> Any:
    from custom_components.climate_advisor import operation_timeline as ot

    open_step = ot.TimelineStep(
        ts=now - timedelta(minutes=30),
        actor=ot.Actor.YOU,
        kind=ot.StepKind.WINDOWS_OPEN,
        reason="windows_open.free_cooling",
        text="Open windows",
        meta={"id": "w-open", "status": "planned"},
    )
    close_step = ot.TimelineStep(
        ts=now + timedelta(hours=2),
        actor=ot.Actor.YOU,
        kind=ot.StepKind.WINDOWS_CLOSE,
        reason="windows_close.outdoor_rise",
        text="Close windows",
        meta={"id": "w-close", "status": "planned"},
    )
    return _TL(steps=(open_step, close_step))


@dataclass(frozen=True)
class _TL:
    steps: tuple


class TestPlanOpenStep:
    def test_sentence_registered(self):
        assert "held.direction_veto.humid" in SENTENCES

    def test_humid_holds_the_running_open_step(self, monkeypatch):
        from custom_components.climate_advisor import timeline_surfaces as ts

        now = datetime(2026, 7, 15, 19, 0, tzinfo=UTC)
        tl = _open_step_timeline(now)
        monkeypatch.setattr(ts, "card_user_step", lambda t, n, windows_open: t.steps[0])
        assert apply_direction_veto(tl, now, _live()) is tl  # control: dry, cooler outside -> the step stands
        held = apply_direction_veto(tl, now, _live(humid_dp_f=66.9))
        step = held.steps[0]
        assert step.status == "held" and step.meta["hold"] == "held.direction_veto.humid"
        assert "humid" in step.text and "67°F" in step.text

    def test_open_window_close_step_says_humid(self, monkeypatch):
        from custom_components.climate_advisor import timeline_surfaces as ts

        now = datetime(2026, 7, 15, 19, 0, tzinfo=UTC)
        tl = _open_step_timeline(now)
        monkeypatch.setattr(ts, "card_user_step", lambda t, n, windows_open: t.steps[1])
        monkeypatch.setattr(ts, "apply_direction_veto", lambda t, n, live: t)
        msg, _step = window_card_msg(tl, now, _live(windows_open=True, humid_dp_f=66.9))
        assert msg.key == "timeline.card.close_humid"
        msg, _step = window_card_msg(tl, now, _live(windows_open=True))
        assert msg.key != "timeline.card.close_humid"

    def test_briefing_window_facts_name_the_humid_veto(self, monkeypatch):
        from custom_components.climate_advisor import timeline_surfaces as ts

        o = SimpleNamespace(status="held", meta={"hold_kind": ts.DIRECTION_VETO, "hold": "held.direction_veto.humid"})
        c = SimpleNamespace(ts=datetime(2026, 7, 15, 22, 0, tzinfo=UTC))
        monkeypatch.setattr(ts, "window_periods", lambda tl, day: [(o, c)])
        assert timeline_windows(object(), None).vetoed == "humid"


# ── night strategy: tonight's open-window options withheld on the live latch ──────────────────────────────────────


class TestNightStrategy:
    def test_open_window_options_withheld(self):
        plan = ns.plan_night(warm_night())
        assert any(ns._SPECS[o.key].windows for o in plan.options)  # control: a warm night offers them
        assert ns.withhold_outdoor_air(plan, None) is plan
        humid = ns.withhold_outdoor_air(plan, 66.9)
        assert humid.options and not any(ns._SPECS[o.key].windows for o in humid.options)
        assert humid.recommended in {o.key for o in humid.options}
        assert not ns._SPECS[humid.recommended].windows
        withheld = [h for h in humid.hidden if h.get("humid_air")]
        assert {h["key"] for h in withheld} == {o.key for o in plan.options if ns._SPECS[o.key].windows}
        assert "recommended" in humid.to_dict() and all(
            not ns._SPECS[o["key"]].windows for o in humid.to_dict()["options"]
        )

    def test_blower_survives(self):
        plan = ns.plan_night(warm_night(fan_mode="hvac_fan"))
        humid = ns.withhold_outdoor_air(plan, 66.9)
        assert {o.key for o in humid.options} <= {ns.OPT_CLOSED, ns.OPT_BLOWER_2H}
        assert ns.OPT_CLOSED in {o.key for o in humid.options}

    def test_coordinator_applies_the_live_latch(self):
        coord_mod = importlib.import_module("custom_components.climate_advisor.coordinator")
        coord = object.__new__(coord_mod.ClimateAdvisorCoordinator)
        coord.config = {"temp_unit": "fahrenheit"}
        coord.hass = None
        coord._timeline_night = {"strategy": ns.plan_night(warm_night()), "reason": "ok"}
        coord._humid_blocking_dp = lambda now=None: 66.9
        body, reason = coord.night_strategy_payload()
        assert reason == "ok"
        assert not any(ns._SPECS[o["key"]].windows for o in body["options"])


# ── the windows gate on a stub coordinator: the verdict from the facts, both clauses ──────────────────────────────


def _gate_stub(indoor_dp: float | None, outdoor_dp: float | None, **config: Any) -> Any:
    coord_mod = importlib.import_module("custom_components.climate_advisor.coordinator")
    coord = object.__new__(coord_mod.ClimateAdvisorCoordinator)
    c = DayClassification(
        day_type="warm",
        trend_direction="stable",
        trend_magnitude=0.0,
        today_high=80.0,
        today_low=58.0,
        tomorrow_high=80.0,
        tomorrow_low=58.0,
    )
    c.windows_recommended = True
    coord._current_classification = c
    coord._last_outdoor_temp = 70.0
    coord._get_indoor_temp = MagicMock(return_value=76.0)
    coord._schedule_planned_window_exit_check = MagicMock()
    coord.config = {"comfort_cool": 75.0, "comfort_heat": 68.0, **config}
    coord.engine_facts = lambda now: EngineFacts(
        as_of=now, indoor_dew_point_f=indoor_dp, outdoor_dew_point_f=outdoor_dp
    )
    coord.hass = None
    coord.automation_engine = _rule_engine(coord)
    return coord


def _rule_engine(coord: Any) -> Any:
    """A real ``AutomationEngine`` carrying only what its ``humid_air_verdict`` reads (the config and the facts the
    coordinator passes in), so the stub-based tests run the production rule, not a copy (REVIEW-W2B-D L2)."""
    from custom_components.climate_advisor.automation import AutomationEngine

    engine = object.__new__(AutomationEngine)
    engine.config = coord.config
    engine._inputs = SimpleNamespace(build_facts=coord.engine_facts)
    return engine


class TestWindowsGate:
    @pytest.mark.parametrize(
        ("indoor_dp", "outdoor_dp", "withdrawn"),
        [
            (55.0, 66.9, True),  # ceiling
            (55.0, 58.0, True),  # wetter than the house (58 vs 55)
            (61.0, 58.0, False),  # drier than the house
            (40.0, 50.0, False),  # dry night: wetter than a dry house, but below 55 F
            (None, 66.9, True),  # indoor unknown: the ceiling still blocks
            (None, 58.0, False),  # indoor unknown: no relative clause
            (55.0, None, False),  # outdoor unknown: nothing blocks
        ],
    )
    def test_gate_reads_the_shared_verdict(self, indoor_dp, outdoor_dp, withdrawn):
        coord = _gate_stub(indoor_dp, outdoor_dp)
        coord._apply_outdoor_windows_gate()
        assert coord._current_classification.windows_recommended is (not withdrawn)
        assert coord._schedule_planned_window_exit_check.called is withdrawn

    def test_guard_off(self):
        coord = _gate_stub(55.0, 66.9, **{CONF_HUMID_AIR_GUARD_ENABLED: False})
        coord._apply_outdoor_windows_gate()
        assert coord._current_classification.windows_recommended is True

    def test_direction_rule_says_no_on_humid_air(self):
        coord = _gate_stub(55.0, 66.9)
        coord._learned_swings = lambda: (1.0, 1.0)
        assert coord._outdoor_direction_ok(70.0, 76.0, 68.0, 75.0) is False
        dry = _gate_stub(55.0, 45.0)
        dry._learned_swings = lambda: (1.0, 1.0)
        assert dry._outdoor_direction_ok(70.0, 76.0, 68.0, 75.0) is True

    @pytest.mark.parametrize(
        ("unit", "fan_mode", "shown"),
        [
            (
                "fahrenheit",
                "disabled",
                "No open-window advice: humid outside (dew point 66.9°F, at or above the ceiling 65.0°F)",
            ),
            (
                "celsius",
                "disabled",
                "No open-window advice: humid outside (dew point 19.4°C, at or above the ceiling 18.3°C)",
            ),
            (
                "fahrenheit",
                "whole_house_fan",
                "Fan not started, no open-window advice: humid outside "
                "(dew point 66.9°F, at or above the ceiling 65.0°F)",
            ),
        ],
    )
    def test_one_status_line_names_the_withheld_window_advice(self, unit, fan_mode, shown):
        """B2: ONE humid-air Status line (#1691's ``humid_air_guard``, one element id on both dashboards); with the
        window advice withdrawn it says so, and a fan-less install gets the line too (only the advice is withheld)."""
        from custom_components.climate_advisor.api import humid_air_view

        with _session(fan_mode=fan_mode, fan_entity="fan.whf") as session:
            _air(session, "17:06", HUMID, hold_min=16)
            line = humid_air_view(session.engine, unit)
            assert line is not None and line["code"] == "ceiling" and line["text"] == shown
            _air(session, "17:30", DRY, hold_min=20)
            assert humid_air_view(session.engine, unit) is None


# ── H5: the briefing and the AI context (Issue #1696) ──────────────────────────────────────────────────────────────


def _briefing(humidity: BriefingHumidity | None, *, unit: str = "fahrenheit") -> str:
    c = DayClassification(
        day_type="warm",
        trend_direction="stable",
        trend_magnitude=1.0,
        today_high=82.0,
        today_low=62.0,
        tomorrow_high=82.0,
        tomorrow_low=62.0,
    )
    c.windows_recommended = False
    return generate_briefing(
        classification=c,
        comfort_heat=70.0,
        comfort_cool=75.0,
        setback_heat=60.0,
        setback_cool=80.0,
        wake_time=time(6, 30),
        sleep_time=time(22, 30),
        temp_unit=unit,
        humidity=humidity,
    )


class TestBriefing:
    def test_dew_points_and_the_decisions_the_guard_changed(self, frozen_now):
        frozen_now(datetime(2026, 7, 15, 6, 0, tzinfo=UTC))
        text = _briefing(
            BriefingHumidity(
                indoor_dp_f=55.0,
                indoor_rh=50.0,
                outdoor_dp_f=66.93,
                outdoor_rh=90.0,
                blocking="ceiling",
                ceiling_f=65.0,
                fan_starts_held_today=2,
                close_notices_today=1,
            )
        )
        assert "💧 Humidity" in text
        assert "Indoors: dew point 55°F (50% relative humidity)." in text
        assert "Outdoors: dew point 67°F (90% relative humidity)." in text
        assert "too humid to bring in right now (dew point 67°F, at or above your 65°F limit)" in text
        # renders once #1691 emits nat_vent_humid_skip (one per humid spell)
        assert "Since midnight the fan stayed off during 2 humid spells" in text
        assert "Since midnight I asked you to close the windows once" in text
        assert "Windows: Closed for now — humid outside (dew point 67°F); I'll update the plan if it dries out" in text

    def test_no_humidity_no_section(self, frozen_now):
        frozen_now(datetime(2026, 7, 15, 6, 0, tzinfo=UTC))
        assert "Humidity" not in _briefing(None)
        assert "Humidity" not in _briefing(BriefingHumidity())
        assert "humid outside" not in _briefing(None)

    def test_dry_day_shows_dew_points_only(self, frozen_now):
        frozen_now(datetime(2026, 7, 15, 6, 0, tzinfo=UTC))
        text = _briefing(BriefingHumidity(indoor_dp_f=55.0, outdoor_dp_f=45.0, ceiling_f=65.0))
        assert "Outdoors: dew point 45°F." in text
        assert "too humid" not in text and "humid outside" not in text

    def test_celsius_twin(self, frozen_now):
        frozen_now(datetime(2026, 7, 15, 6, 0, tzinfo=UTC))
        text = _briefing(
            BriefingHumidity(indoor_dp_f=55.0, outdoor_dp_f=66.93, blocking="wetter_than_house", ceiling_f=65.0),
            unit="celsius",
        )
        assert "Outdoors: dew point 19°C." in text
        assert "wetter than the air in the house" in text

    def test_coordinator_counts_todays_guard_decisions(self):
        coord_mod = importlib.import_module("custom_components.climate_advisor.coordinator")
        coord = object.__new__(coord_mod.ClimateAdvisorCoordinator)
        now = datetime(2026, 7, 15, 20, 0, tzinfo=UTC)
        coord._event_log = [
            {"time": "2026-07-14T23:00:00+00:00", "type": "humid_windows_close_notice"},  # yesterday
            {"time": "2026-07-15T19:00:00+00:00", "type": "humid_windows_close_notice"},
            {"time": "2026-07-15T19:05:00+00:00", "type": "nat_vent_humid_skip"},
            {"time": "2026-07-15T19:10:00+00:00", "type": "nat_vent_humid_skip"},
        ]
        assert coord.humid_decisions_today(now) == {"fan_starts_held": 2, "close_notices": 1}


class TestAIContext:
    def test_current_state_names_dew_points_and_the_guard(self):
        from custom_components.climate_advisor import ai_skills_context as m

        coord = SimpleNamespace(
            config={"temp_unit": "fahrenheit"},
            humidity_display=lambda: {
                "indoor": {"dp_f": 55.0, "rh": 50.0, "source": "x"},
                "outdoor": {"dp_f": 66.93, "rh": 90.0, "source": "y"},
                "guard": {"blocks": True, "reason": "ceiling", "ceiling_f": 65.0},
            },
            humid_decisions_today=lambda: {"fan_starts_held": 3, "close_notices": 1},
        )
        lines = "\n".join(m._moisture_lines(coord))
        assert "indoor_dew_point:    55.0°F (RH 50%)" in lines
        assert "outdoor_dew_point:   66.9°F (RH 90%)" in lines
        assert "blocking (ceiling; ceiling 65.0°F): open-window advice withdrawn" in lines
        assert "close-window notices=1 fan starts held=3" in lines
        assert m._moisture_lines(SimpleNamespace()) == []

    def test_activity_row_for_the_notice(self):
        from custom_components.climate_advisor.ai_skills_context import EVENT_RENDERERS

        label, detail = EVENT_RENDERERS["humid_windows_close_notice"](
            {"reason": "ceiling", "outdoor_dew_point": 66.9, "indoor_dew_point": 55.0, "ceiling": 65.0}, "fahrenheit"
        )
        assert "close the windows" in str(label) and "66.9°F" in str(label)
        assert "65.0°F" in str(detail)


def test_dew_point_fixture_values():
    """The scenario constants mean what the tests say (Magnus, the one dew-point function)."""
    assert dew_point_f(70.0, 90.0) == pytest.approx(66.93, abs=0.01)
    assert dew_point_f(62.0, 50.0) < 55.0
    assert dew_point_f(75.0, 50.0) == pytest.approx(55.1, abs=0.2)
    assert math.isfinite(dew_point_f(23.9 * 9 / 5 + 32, 50.0))


class TestEdges:
    """Branches the scenarios above do not reach (each still describes an occupant outcome)."""

    def test_night_with_only_open_window_options_has_no_pick(self):
        import dataclasses

        plan = ns.plan_night(warm_night())
        only_open = dataclasses.replace(plan, options=tuple(o for o in plan.options if ns._SPECS[o.key].windows))
        humid = ns.withhold_outdoor_air(only_open, 66.9)
        assert humid.options == () and humid.recommended is None  # nothing to advise, never "open the windows"

    @pytest.mark.parametrize(
        ("caps", "mode", "pre_pause", "key"),
        [
            ((True, False), "heat", None, "notify.humid_windows_close_neutral"),  # heat-only thermostat
            ((True, True), "heat", None, "notify.humid_windows_close_neutral"),  # can cool, but heating
            ((True, True), "off", "heat", "notify.humid_windows_close_neutral"),  # paused from heat
            ((True, True), "off", "cool", "notify.humid_windows_close"),  # paused from cool: the AC takes over
            ((True, True), "heat_cool", None, "notify.humid_windows_close"),
        ],
    )
    def test_ac_wording_only_when_the_thermostat_will_cool(self, caps, mode, pre_pause, key):
        coord = _gate_stub(55.0, 66.9)
        coord.data = {}
        coord.async_update_listeners = MagicMock()
        coord._any_sensor_open_and_settled = lambda: True
        coord._startup_coalesce_active = False
        coord._occupancy_mode = "home"
        coord._humid_notice_episode = False
        coord._humid_notice_last_at = None
        coord.automation_engine = SimpleNamespace(
            humid_air_verdict=coord.automation_engine.humid_air_verdict,  # the production rule
            _get_thermostat_capabilities=lambda: SimpleNamespace(supports_heat=caps[0], supports_cool=caps[1]),
            _paused_by_door=pre_pause is not None,
            _pre_pause_mode=pre_pause,
            _pre_fan_hvac_mode=None,
            _natural_vent_active=False,
        )
        coord._thermostat_hvac_mode = lambda: mode
        coord._emit_event = MagicMock()
        coord._notify_in_background = MagicMock()
        coord._tick_humid_windows_notice(datetime(2026, 7, 15, 20, 0, tzinfo=UTC))
        assert coord._emit_event.call_count == 1
        assert coord._notify_in_background.call_args.args[1].key == key

    def test_blower_advice_already_sent_this_session_suppresses_the_notice(self):
        coord = _gate_stub(55.0, 66.9)
        coord.data = {}
        coord.async_update_listeners = MagicMock()
        coord._any_sensor_open_and_settled = lambda: True
        coord._startup_coalesce_active = False
        coord._occupancy_mode = "home"
        coord._humid_notice_episode = False
        coord._humid_notice_last_at = None
        coord.automation_engine = SimpleNamespace(
            humid_air_verdict=coord.automation_engine.humid_air_verdict,
            _natural_vent_active=True,
            _close_windows_advised=True,
        )
        coord._emit_event = MagicMock()
        coord._notify_in_background = MagicMock()
        coord._tick_humid_windows_notice(datetime(2026, 7, 15, 20, 0, tzinfo=UTC))
        assert not coord._emit_event.called and not coord._notify_in_background.called
        assert coord._humid_notice_episode is True  # marked: no retry every tick

    def test_humid_notice_suppresses_the_blower_advice_of_the_same_session(self):
        from custom_components.climate_advisor.automation import AutomationEngine, close_windows_advice_deferred

        assert close_windows_advice_deferred("away") and close_windows_advice_deferred("vacation")
        assert not close_windows_advice_deferred("home")
        engine = object.__new__(AutomationEngine)
        engine._natural_vent_active = True
        engine.config = {"fan_mode": "hvac_fan", "comfort_cool": 74}
        engine._close_windows_advised = True  # set by the humid notice
        engine._occupancy_mode = "home"
        engine._get_indoor_temp_f = lambda: 80.0
        engine._notify = MagicMock()
        import asyncio

        asyncio.run(engine._advise_close_windows_if_too_warm())
        assert not engine._notify.called

    def test_verdict_failure_reads_as_not_blocking(self):
        coord = _gate_stub(55.0, 66.9)

        def _boom(now):
            raise RuntimeError("facts unavailable")

        coord.engine_facts = _boom
        assert coord._humid_air_verdict() is None
        assert coord._humid_blocking_dp() is None and coord._humid_blocks() is None
        coord._apply_outdoor_windows_gate()
        assert coord._current_classification.windows_recommended is True

    def test_ai_lines_when_clear_unknown_or_off(self):
        from custom_components.climate_advisor import ai_skills_context as m

        def _coord(guard: dict, indoor: dict) -> Any:
            return SimpleNamespace(
                config={"temp_unit": "celsius"},
                humidity_display=lambda: {"indoor": indoor, "outdoor": {"dp_f": None}, "guard": guard},
            )

        clear = "\n".join(m._moisture_lines(_coord({"blocks": False, "ceiling_f": 65.0}, {"dp_f": 55.0, "rh": None})))
        assert "indoor_dew_point:    12.8°C" in clear and "RH" not in clear.split("\n")[0]
        assert "outdoor_dew_point:   unknown" in clear and "clear (ceiling 18.3°C)" in clear
        off = "\n".join(m._moisture_lines(_coord({"blocks": False, "ceiling_f": None}, {})))
        assert "humid_air_guard:     off" in off

        def _raise():
            raise RuntimeError("no facts")

        assert m._moisture_lines(SimpleNamespace(humidity_display=_raise)) == []
        assert m._moisture_lines(SimpleNamespace(humidity_display=lambda: "not a dict")) == []

    def test_humidity_display_reads_the_decision_facts(self):
        with _session() as session:
            _air(session, "17:06", HUMID, hold_min=16)
            shown = session.coordinator.humidity_display()
            assert shown["outdoor"]["dp_f"] == pytest.approx(dew_point_f(70.0, 90.0), abs=0.05)
            assert shown["outdoor"]["rh"] == pytest.approx(90.0)
            assert shown["indoor"]["dp_f"] == pytest.approx(dew_point_f(75.0, 50.0), abs=0.05)
            assert shown["guard"] == {
                "blocks": True,
                "window_plan_withdrawn": False,  # no window period planned today
                "reason": "ceiling",
                "ceiling_f": 65.0,
            }
            facts = session.coordinator._briefing_humidity()
            assert facts is not None and facts.blocking == "ceiling" and facts.outdoor_rh == pytest.approx(90.0)

    def test_shown_briefing_follows_the_guard(self):
        """The briefing written on a dry morning is rewritten when the guard engages (and not on every cycle)."""
        with _session() as session:
            c = session.coordinator
            _window_day(session, "17:05")
            _air(session, "17:06", DRY)
            cls = c._current_classification
            c._briefing_sent_today = True
            c._briefing_day_type = cls.day_type
            c._briefing_today_high = cls.today_high
            c._briefing_windows_recommended = cls.forecast_windows_recommended
            plan = getattr(c, "_nat_vent_plan", None) or {}
            c._briefing_nat_vent_cutoff = plan.get("nat_vent_cutoff")
            c._briefing_nat_vent_cutoff_reason = plan.get("nat_vent_cutoff_reason")
            c._briefing_evening_open_time = plan.get("evening_open_time")
            c._briefing_tou_bank = c._briefing_tou_bank_key()
            c._briefing_humid_blocking = False
            assert c._maybe_regenerate_briefing_for_drift() is False  # dry: nothing to rewrite
            _air(session, "17:20", HUMID, hold_min=16)
            assert c._maybe_regenerate_briefing_for_drift() is True
            assert c._briefing_humid_blocking is True
            assert "humid outside" in str(c._last_briefing_short)
            assert c._maybe_regenerate_briefing_for_drift() is False  # once per flip
            # A2 (mirror): the guard releases on dry air -> the shown text is rewritten back to a window plan
            _air(session, "18:00", DRY, hold_min=20)
            assert c._humid_blocks() is None
            assert c._maybe_regenerate_briefing_for_drift() is True
            assert c._briefing_humid_blocking is False
            assert "humid outside" not in str(c._last_briefing_short)
            assert c._maybe_regenerate_briefing_for_drift() is False

    @pytest.mark.parametrize("stored", [True, False, "missing"])
    def test_briefing_humid_baseline_survives_a_restart(self, stored):
        """A3/B9: the flag the shown briefing was written with is saved and restored through the real save / restore
        path, so a restart mid-day keeps following the guard; a state saved before 0.9.74 (no flag) reads as
        "written while not blocking"."""
        from tools.sim_harness._loop import run_coro

        with _session() as session:
            c = session.coordinator
            c._briefing_humid_blocking = stored is True
            saved = c._build_state_dict()
            if stored == "missing":
                saved["briefing_state"].pop("briefing_humid_blocking")
            else:
                assert saved["briefing_state"]["briefing_humid_blocking"] is stored
            c._briefing_humid_blocking = None
            c._state_persistence.load = lambda: saved
            run_coro(c.async_restore_state())
            assert c._briefing_humid_blocking is (stored is True)

    def test_a_briefing_stored_by_0_9_73_still_renders(self, frozen_now):
        """B1: today's briefing saved before the upgrade has no humidity slot; restoring it must not show a raw key."""
        from custom_components.climate_advisor.briefing import generate_briefing_msg

        coord_mod = importlib.import_module("custom_components.climate_advisor.coordinator")
        frozen_now(datetime(2026, 7, 15, 6, 0, tzinfo=UTC))
        c = DayClassification(
            day_type="cold",
            trend_direction="stable",
            trend_magnitude=1.0,
            today_high=45.0,
            today_low=30.0,
            tomorrow_high=45.0,
            tomorrow_low=30.0,
        )
        msg = generate_briefing_msg(
            classification=c,
            comfort_heat=70.0,
            comfort_cool=75.0,
            setback_heat=60.0,
            setback_cool=80.0,
            wake_time=time(6, 30),
            sleep_time=time(22, 30),
        )
        wire = msg.to_wire()
        old = {**wire, "params": {k: v for k, v in wire["params"].items() if k not in ("has_humidity", "humidity")}}
        assert old["key"] == "briefing.full.cold" and "has_humidity" not in old["params"]
        text = coord_mod._restore_briefing_text("stored English", old)
        for lang in ("en", "de"):
            rendered = i18n.render(text.msg, lang)
            assert not rendered.startswith("briefing.full") and "========" in rendered


class TestNoFanStartPromisedIntoHumidAir:
    """REVIEW-W2A-C N2: on a humid night the plan neither promises a CA fan start for a window period nor tells the
    occupant to run the fan / open the windows themselves (the night strategy's own fan steps)."""

    def test_timeline_drops_the_window_fan_start_while_humid(self):
        from custom_components.climate_advisor.operation_timeline import StepKind

        def _starts(session: FakeHassSession) -> list:
            session.coordinator._timeline_shadow_run("cycle")
            tl = session.coordinator._surface_timeline()
            assert tl is not None
            return [s for s in tl.steps if s.kind == StepKind.NAT_VENT_START]

        day = {
            "type": "classification",
            "day_type": "warm",
            "hvac_mode": "off",
            "windows_recommended": True,
            "window_open_time": "19:00",
            "window_close_time": "22:00",
        }
        with _session(fan_mode="whole_house_fan", fan_entity="fan.whf") as session:
            _go(session, "17:05", day)
            _air(session, "17:06", DRY, hold_min=20)
            assert _starts(session)  # control: a dry evening promises the fan session with the window period
            _air(session, "17:30", HUMID, hold_min=20)
            assert _starts(session) == []
            _air(session, "18:00", DRY, hold_min=20)  # the latch releases: the promise comes back on the next plan
            _go(session, "18:21", day)
            assert _starts(session)

    def test_a_running_fan_session_keeps_its_stop_step_while_humid(self):
        """L4 (D4 half): a session already running when the guard engages is not ended by humidity, so the plan still
        says when Climate Advisor stops the fan; only new starts are withheld (``promise_start`` vs ``running``)."""
        import dataclasses

        from custom_components.climate_advisor.operation_timeline import build_timeline
        from tests.test_operation_timeline import _evening_cooling

        running = _evening_cooling(windows_physically_open=True, nat_vent_active=True)
        for humid in (False, True):
            tl = build_timeline(dataclasses.replace(running, humid_air_blocks=humid))
            assert any(c.kind == "open_now" and c.accepted for c in tl.candidates)
            assert [s for s in tl.steps if s.kind == "nat_vent_stop"], f"stop step lost (humid={humid})"
        # a planned (not yet running) evening period: the humid plan promises neither the start nor its stop
        planned = _evening_cooling()
        dry = build_timeline(planned)
        wet = build_timeline(dataclasses.replace(planned, humid_air_blocks=True))
        assert [s for s in dry.steps if s.kind == "nat_vent_start"]
        assert not [s for s in wet.steps if s.kind in ("nat_vent_start", "nat_vent_stop")]

    def test_night_strategy_gives_no_fan_or_window_advice_while_humid(self, monkeypatch):
        from tests.test_night_strategy import END, START, STUB, _timeline

        monkeypatch.setattr(ns, "_timeline_module", lambda: STUB)
        import dataclasses

        # a night whose pick runs the whole-house fan (the warm test night offers it)
        plan = dataclasses.replace(ns.plan_night(warm_night()), recommended=ns.OPT_FAN_2H)
        you = {STUB.StepKind.FAN_ON, STUB.StepKind.WINDOWS_OPEN}

        def _advice(strategy: ns.NightStrategy) -> list:
            tl = ns.apply_night_strategy(_timeline(), strategy, start=START, end=END)  # no CA nat-vent step tonight
            return [s for s in tl.steps if s.actor == STUB.Actor.YOU and s.kind in you]

        assert _advice(plan)  # control: a dry warm night tells the occupant to open up / run the fan
        assert _advice(ns.withhold_outdoor_air(plan, 66.9)) == []
