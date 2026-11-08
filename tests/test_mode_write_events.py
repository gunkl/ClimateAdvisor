"""Issue #1073 (S8): the activity record explains every thermostat mode change and every hold.

Occupant framing: before this change a heat/cool flip showed up only in the HA log, and a settling-guard
hold (the furnace deliberately NOT starting a minute after the AC stopped) left no trace at all. The
occupant, or whoever debugs the next "why did it heat?", now sees one `thermostat_mode_set` row per REAL
mode change (who asked, why, indoor and band), one `comfort_switch_held` row per hold, a
`comfort_family_switch` row even when the previous mode was unknown, and the control-quality report gets a
capped, persisted log of hold episodes.

Real ``AutomationEngine`` + a fake thermostat. Nothing here re-implements production logic.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.climate_advisor.automation import AutomationEngine
from custom_components.climate_advisor.classifier import DayClassification
from custom_components.climate_advisor.const import COMFORT_HOLD_LOG_CAP

_AUTOMATION = "custom_components.climate_advisor.automation"
_T0 = datetime(2026, 10, 11, 14, 0, 0, tzinfo=UTC)
_BOTH = ["off", "heat", "cool"]
_HVAC_MODE_FOR_DAY = {"hot": "cool", "warm": "off", "mild": "off", "cool": "off", "cold": "heat"}


def _consume_coroutine(coro):
    coro.close()


def _run(coro):
    return asyncio.run(coro)


class _Clock:
    def __init__(self, start: datetime = _T0) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kw) -> None:
        self.now += timedelta(**kw)


def _make_engine(*, indoor: float, mode: str, follow_commands: bool = True, **cfg) -> AutomationEngine:
    hass = MagicMock()
    hass.async_create_task = MagicMock(side_effect=_consume_coroutine)
    hass.states = MagicMock()
    state = MagicMock()
    state.state = mode
    state.attributes = {
        "hvac_modes": list(_BOTH),
        "supported_features": 1,
        "current_temperature": indoor,
        "friendly_name": "Hallway",
    }
    hass.states.get.return_value = state

    async def _call(domain, service, data, **_kw):
        if domain != "climate" or not follow_commands:
            return None
        if service == "set_hvac_mode":
            state.state = data["hvac_mode"]
        elif service == "set_temperature":
            if "hvac_mode" in data:
                state.state = data["hvac_mode"]
            state.attributes["temperature"] = data["temperature"]
        return None

    hass.services = MagicMock()
    hass.services.async_call = AsyncMock(side_effect=_call)
    config = {
        "comfort_heat": 68.0,
        "comfort_cool": 74.0,
        "setback_heat": 62.0,
        "setback_cool": 80.0,
        "notify_service": "notify.notify",
        "temp_unit": "fahrenheit",
        **cfg,
    }
    engine = AutomationEngine(
        hass=hass,
        climate_entity="climate.thermostat",
        weather_entity="weather.forecast_home",
        door_window_sensors=[],
        notify_service=config["notify_service"],
        config=config,
    )
    engine.inputs.thermal = {}
    engine._test_state = state
    engine.events = []
    engine._emit_event_callback = lambda name, payload: engine.events.append((name, payload))
    return engine


def _classification(day_type: str) -> DayClassification:
    obj = object.__new__(DayClassification)
    obj.day_type = day_type
    obj.hvac_mode = _HVAC_MODE_FOR_DAY[day_type]
    obj.trend_direction = "stable"
    obj.trend_magnitude = 1.0
    obj.today_high = 95.0
    obj.today_low = 70.0
    obj.tomorrow_high = 95.0
    obj.tomorrow_low = 70.0
    obj.pre_condition = False
    obj.pre_condition_target = None
    obj.windows_recommended = False
    obj.window_open_time = None
    obj.window_close_time = None
    obj.setback_modifier = 0.0
    return obj


def _events(engine: AutomationEngine, name: str) -> list[dict]:
    return [p for n, p in engine.events if n == name]


def _set_indoor(engine: AutomationEngine, value: float) -> None:
    engine._test_state.attributes["current_temperature"] = value


def _stamp_ran(engine: AutomationEngine, direction: str, ago_min: float, now: datetime) -> None:
    iso = (now - timedelta(minutes=ago_min)).isoformat()
    if direction == "heat":
        engine._last_hvac_heating_active = iso
    else:
        engine._last_hvac_cooling_active = iso


def _apply(engine: AutomationEngine, clock: _Clock, day: str, indoor: float | None = None) -> None:
    c = _classification(day)
    engine._current_classification = c
    if indoor is not None:
        _set_indoor(engine, indoor)
    with patch(f"{_AUTOMATION}.dt_util.now", side_effect=clock):
        _run(engine.apply_classification(c))


def _set_mode(engine: AutomationEngine, clock: _Clock, mode: str, *, reason: str, context: str = "restore") -> None:
    with patch(f"{_AUTOMATION}.dt_util.now", side_effect=clock), engine._mode_change_context(context):
        _run(engine._set_hvac_mode(mode, reason=reason))


# ---------------------------------------------------------------------------
# thermostat_mode_set
# ---------------------------------------------------------------------------


class TestThermostatModeSet:
    def test_one_row_per_real_change_with_the_full_payload(self):
        engine = _make_engine(indoor=71.0, mode="off")
        clock = _Clock()
        engine._last_commanded_hvac_mode = "off"
        _set_mode(engine, clock, "heat", reason="door/window closed -- restoring heat mode")
        rows = _events(engine, "thermostat_mode_set")
        assert len(rows) == 1
        row = rows[0]
        assert row["zone"] == "thermostat", "zone is the generic redacted label"
        assert "climate.thermostat" not in json.dumps(row)
        assert "Hallway" not in json.dumps(row), "the friendly name must never leak"
        assert row["from_mode"] == "off"
        assert row["to_mode"] == "heat"
        assert row["reason"] == "restoring after a pause", "a FIXED label, never the caller's free text"
        assert row["source"] == "restore"
        assert row["indoor_f"] == 71.0
        assert row["outcome"] == "requested", "recorded before the climate call, so never claims it was applied"
        assert set(row) == {
            "zone",
            "from_mode",
            "to_mode",
            "reason",
            "source",
            "indoor_f",
            "floor_f",
            "ceiling_f",
            "outcome",
        }

    def test_re_affirming_the_mode_the_thermostat_is_already_in_emits_nothing(self):
        engine = _make_engine(indoor=71.0, mode="heat")
        clock = _Clock()
        _set_mode(engine, clock, "heat", reason="re-assert")
        assert _events(engine, "thermostat_mode_set") == []
        engine._last_commanded_hvac_mode = "heat"
        _set_mode(engine, clock, "heat", reason="re-assert again")
        assert _events(engine, "thermostat_mode_set") == []

    def test_each_writer_is_labelled_with_its_source(self):
        engine = _make_engine(indoor=71.0, mode="off")
        clock = _Clock()
        engine._last_commanded_hvac_mode = "off"
        modes = ["heat", "cool", "heat", "cool"]
        sources = ["tou", "ode_guard", "restore", "nat_vent"]
        for mode, source in zip(modes, sources, strict=True):
            clock.advance(minutes=5)
            _set_mode(engine, clock, mode, reason=f"why-{source}", context=source)
        rows = _events(engine, "thermostat_mode_set")
        assert [r["source"] for r in rows] == sources
        assert [r["to_mode"] for r in rows] == modes
        assert [r["from_mode"] for r in rows] == ["off", "heat", "cool", "heat"]

    def test_selector_write_via_the_comfort_band_is_attributed_to_the_selector(self):
        engine = _make_engine(indoor=66.0, mode="off")
        _apply(engine, _Clock(), "mild")
        rows = _events(engine, "thermostat_mode_set")
        assert [r["to_mode"] for r in rows] == ["heat"]
        assert rows[0]["source"] == "selector"
        assert rows[0]["floor_f"] == 68.0 and rows[0]["ceiling_f"] == 74.0

    def test_user_change_is_a_row_with_source_user(self):
        engine = _make_engine(indoor=71.0, mode="heat")
        engine._append_mode_change("off", "heat", "user")
        rows = _events(engine, "thermostat_mode_set")
        assert len(rows) == 1
        assert rows[0]["source"] == "user" and rows[0]["reason"] == "you changed it at the thermostat"
        assert rows[0]["from_mode"] == "off"

    def test_identical_change_inside_120_s_is_not_announced_twice(self):
        engine = _make_engine(indoor=71.0, mode="heat")
        clock = _Clock()
        with patch(f"{_AUTOMATION}.dt_util.now", side_effect=clock):
            engine._append_mode_change("off", "heat", "user")
            clock.advance(seconds=60)
            engine._append_mode_change("off", "heat", "user")
            assert len(_events(engine, "thermostat_mode_set")) == 1
            clock.advance(seconds=120)
            engine._append_mode_change("off", "heat", "user")
        assert len(_events(engine, "thermostat_mode_set")) == 2
        assert len(engine.comfort_mode_changes) == 3, "the ring buffer (the clock) records every change"

    def test_a_raising_callback_never_breaks_the_mode_write(self):
        engine = _make_engine(indoor=71.0, mode="off")
        engine._last_commanded_hvac_mode = "off"

        def boom(*_a, **_k):
            raise RuntimeError("sink down")

        engine._emit_event_callback = boom
        _set_mode(engine, _Clock(), "heat", reason="x")
        assert engine._test_state.state == "heat"
        assert engine.comfort_mode_changes[-1]["to_mode"] == "heat"

    def test_no_callback_is_a_noop(self):
        engine = _make_engine(indoor=71.0, mode="off")
        engine._emit_event_callback = None
        engine._last_commanded_hvac_mode = "off"
        _set_mode(engine, _Clock(), "heat", reason="x")
        assert engine._test_state.state == "heat"


# ---------------------------------------------------------------------------
# comfort_switch_held + Debug-row clearing + comfort_family_switch from unknown
# ---------------------------------------------------------------------------


class TestComfortSwitchHeld:
    def _held_scene(self):
        engine = _make_engine(indoor=67.0, mode="cool")
        clock = _Clock()
        _stamp_ran(engine, "cool", 40.0, clock.now)
        _apply(engine, clock, "mild", 67.0)  # depth 1, tolerance 2, cool ran 40 min ago -> hold
        return engine, clock

    def test_event_once_per_hold_signature_with_the_catalog_payload(self):
        engine, clock = self._held_scene()
        _apply(engine, clock, "mild", 67.0)
        _apply(engine, clock, "mild", 66.5)
        rows = _events(engine, "comfort_switch_held")
        assert len(rows) == 1, "repeats of the same hold are not re-announced"
        row = rows[0]
        assert set(row) == {
            "candidate_family",
            "zone",
            "depth_f",
            "tolerance_f",
            "opposite_ran_min_ago",
            "window_min",
            "indoor_f",
            "floor_f",
            "ceiling_f",
        }
        assert row["candidate_family"] == "heating"
        assert row["zone"] == "breach"
        assert row["depth_f"] == pytest.approx(1.0)
        assert row["tolerance_f"] == pytest.approx(2.0)
        assert row["opposite_ran_min_ago"] == pytest.approx(40.0, abs=1.0)
        assert row["window_min"] == pytest.approx(120.0)
        assert row["floor_f"] == 68.0 and row["ceiling_f"] == 74.0
        assert engine._test_state.state == "cool", "the thermostat was not flipped"

    def test_debug_row_clears_as_soon_as_the_hold_ends(self):
        engine, clock = self._held_scene()
        assert engine._last_comfort_hold is not None
        _apply(engine, clock, "mild", 71.0)  # back inside the band: no longer holding
        assert engine._last_comfort_hold is None

    def test_a_new_hold_after_recovery_is_announced_again(self):
        engine, clock = self._held_scene()
        clock.advance(minutes=11)  # past the 10-minute merge/dedup window: a genuinely new hold
        _apply(engine, clock, "mild", 71.0)
        clock.advance(minutes=11)
        _apply(engine, clock, "mild", 67.0)
        assert len(_events(engine, "comfort_switch_held")) == 2

    def test_a_flickering_reading_is_announced_once_and_stays_one_episode(self, caplog):
        """Issue #1073 (hold flicker): 67 -> 71 -> 67 -> 71 -> 67 inside 10 minutes is ONE hold."""
        engine, clock = self._held_scene()
        with caplog.at_level("INFO"):
            for indoor in (71.0, 67.0, 71.0, 67.0):
                clock.advance(minutes=1)
                _apply(engine, clock, "mild", indoor)
        assert len(_events(engine, "comfort_switch_held")) == 1
        assert len(engine.comfort_holds) == 1, "re-entries within 10 min reopen the previous episode"
        assert engine.comfort_holds[0]["end_ts"] is None
        held_infos = [
            r for r in caplog.records if "Comfort edge switch held" in r.getMessage() and r.levelname == "INFO"
        ]
        assert held_infos == [], "the first INFO was emitted before the flicker; re-entries log at DEBUG only"

    def test_hold_mirror_cool_overshoot_after_heating(self):
        engine = _make_engine(indoor=75.0, mode="heat")
        clock = _Clock()
        _stamp_ran(engine, "heat", 40.0, clock.now)
        _apply(engine, clock, "mild", 75.0)
        rows = _events(engine, "comfort_switch_held")
        assert [r["candidate_family"] for r in rows] == ["cooling"]
        assert engine._test_state.state == "heat"


class TestComfortFamilySwitchFromUnknown:
    def test_switch_is_announced_when_the_prior_mode_was_unknown(self):
        engine = _make_engine(indoor=66.0, mode="off")
        _apply(engine, _Clock(), "mild", 66.0)
        rows = _events(engine, "comfort_family_switch")
        assert len(rows) == 1
        assert rows[0]["from_family"] == "unknown"
        assert rows[0]["resolved_family"] == "heating"
        assert rows[0]["breach"] is True
        assert engine._last_comfort_switch["from"] == "unknown"

    def test_known_prior_family_is_labelled(self):
        engine = _make_engine(indoor=75.0, mode="heat")
        _apply(engine, _Clock(), "mild", 75.0)
        rows = _events(engine, "comfort_family_switch")
        assert [r["from_family"] for r in rows] == ["heating"]
        assert rows[0]["resolved_family"] == "cooling"

    def test_an_unrealised_wish_from_unknown_is_not_re_announced_every_cycle(self):
        engine = _make_engine(indoor=66.0, mode="off", follow_commands=False)  # a thermostat that ignores commands
        clock = _Clock()
        for _ in range(3):
            clock.advance(minutes=30)
            _apply(engine, clock, "mild", 66.0)
        assert len(_events(engine, "comfort_family_switch")) == 1


# ---------------------------------------------------------------------------
# Hold-episode log (S7): episodes, end reasons, persistence, cap
# ---------------------------------------------------------------------------


class TestHoldEpisodes:
    def _held(self, *, confidence=None):
        engine = _make_engine(indoor=67.0, mode="cool")
        if confidence is not None:
            engine.inputs.thermal = {"confidence": confidence}
        clock = _Clock()
        _stamp_ran(engine, "cool", 40.0, clock.now)
        _apply(engine, clock, "mild", 67.0)
        return engine, clock

    def test_episode_opens_with_candidate_tolerance_and_confidence(self):
        engine, clock = self._held(confidence="medium")
        holds = engine.comfort_holds
        assert len(holds) == 1
        h = holds[0]
        assert h["start_ts"] == clock.now.isoformat()
        assert h["end_ts"] is None and h["end_reason"] is None
        assert h["candidate_family"] == "heating"
        assert h["max_depth_f"] == pytest.approx(1.0)
        assert h["tolerance_f"] == pytest.approx(2.0)
        assert h["model_confidence"] == "medium"

    def _held_at(self, indoor):
        engine = _make_engine(indoor=indoor, mode="cool")
        clock = _Clock()
        _stamp_ran(engine, "cool", 40.0, clock.now)
        _apply(engine, clock, "mild", indoor)
        return engine, clock

    def test_zone_is_recorded_from_the_decision(self):
        assert self._held()[0].comfort_holds[0]["zone"] == "breach"  # 67.0 is below the 68 floor
        engine, _ = self._held_at(69.0)  # inside the band, near the floor
        assert engine.comfort_holds[0]["zone"] == "margin"
        assert engine.comfort_holds[0]["max_depth_f"] == pytest.approx(0.0)

    def test_margin_episode_escalates_to_breach_and_never_downgrades(self):
        engine, clock = self._held_at(69.0)
        clock.advance(minutes=2)
        _apply(engine, clock, "mild", 67.0)  # now a real breach, same episode
        assert len(engine.comfort_holds) == 1 and engine.comfort_holds[0]["zone"] == "breach"
        clock.advance(minutes=2)
        _apply(engine, clock, "mild", 69.0)  # back to margin: the breach stays recorded
        assert engine.comfort_holds[0]["zone"] == "breach"
        assert engine.comfort_holds[0]["max_depth_f"] == pytest.approx(1.0)

    def test_restored_unzoned_episode_with_depth_is_not_downgraded_to_margin(self):
        engine, clock = self._held_at(67.0)  # breach, depth 1.0
        engine._comfort_hold_episode["zone"] = None  # as if restored from before zones were recorded
        clock.advance(minutes=2)
        _apply(engine, clock, "mild", 69.0)  # next reading is margin (depth 0.0), same episode
        assert len(engine.comfort_holds) == 1
        assert engine.comfort_holds[0]["zone"] == "breach"
        assert engine.comfort_holds[0]["max_depth_f"] == pytest.approx(1.0)

    def test_zone_survives_a_restart_and_old_rows_load_as_none(self):
        engine, _ = self._held_at(69.0)
        fresh = _make_engine(indoor=70.0, mode="off")
        fresh.restore_state(json.loads(json.dumps(engine.get_serializable_state())))
        assert fresh.comfort_holds[0]["zone"] == "margin"
        legacy = _make_engine(indoor=67.0, mode="cool")
        legacy._restore_comfort_holds(
            [{"start_ts": _T0.isoformat(), "end_ts": None, "candidate_family": "heating", "max_depth_f": 1.0}]
        )
        assert legacy.comfort_holds[0]["zone"] is None
        legacy._restore_comfort_holds(
            [{"start_ts": _T0.isoformat(), "candidate_family": "heating", "zone": "sideways"}]
        )
        assert legacy.comfort_holds[0]["zone"] is None

    def test_unknown_or_missing_model_confidence_is_none(self):
        assert self._held()[0].comfort_holds[0]["model_confidence"] is None
        assert self._held(confidence="bogus")[0].comfort_holds[0]["model_confidence"] is None

    def test_max_depth_follows_the_deepest_reading_of_the_episode(self):
        engine, clock = self._held()
        clock.advance(minutes=5)
        _apply(engine, clock, "mild", 66.0)  # depth 2 == tolerance, still a hold
        clock.advance(minutes=5)
        _apply(engine, clock, "mild", 66.8)
        holds = engine.comfort_holds
        assert len(holds) == 1
        assert holds[0]["max_depth_f"] == pytest.approx(2.0)

    def test_recovered_when_indoor_returns_inside_the_band_without_a_switch(self):
        engine, clock = self._held()
        clock.advance(minutes=10)
        _apply(engine, clock, "mild", 71.0)
        h = engine.comfort_holds[0]
        assert h["end_reason"] == "recovered"
        assert h["end_ts"] == clock.now.isoformat()
        assert engine._test_state.state == "cool"

    def test_switched_when_a_deeper_breach_arms_the_held_family(self):
        engine, clock = self._held()
        clock.advance(minutes=10)
        _apply(engine, clock, "mild", 65.0)  # depth 3 > tolerance 2 -> heat at once
        assert engine._test_state.state == "heat"
        assert engine.comfort_holds[0]["end_reason"] == "switched"

    def test_switched_when_the_window_lapses(self):
        engine, clock = self._held()
        clock.advance(minutes=90)  # cool ran 40 min before t0; now 130 min ago -> window (120) lapsed
        _apply(engine, clock, "mild", 67.0)
        assert engine._test_state.state == "heat"
        assert engine.comfort_holds[0]["end_reason"] == "switched"

    def test_lapsed_when_the_selector_stops_holding_without_a_switch_or_recovery(self):
        engine, clock = self._held()
        clock.advance(minutes=5)
        engine._natural_vent_active = True  # nat-vent now owns the HVAC: the selector steps aside
        _apply(engine, clock, "mild", 67.0)
        h = engine.comfort_holds[0]
        assert h["end_reason"] == "lapsed"
        assert engine._last_comfort_hold is None

    def test_a_second_hold_after_the_first_ended_is_a_second_episode(self):
        engine, clock = self._held()
        clock.advance(minutes=5)
        _apply(engine, clock, "mild", 71.0)
        clock.advance(minutes=11)  # beyond the 10-minute merge window: a second episode
        _apply(engine, clock, "mild", 67.0)
        holds = engine.comfort_holds
        assert [h["end_reason"] for h in holds] == ["recovered", None]

    def test_model_confidence_never_changes_what_the_guard_does(self):
        outcomes = []
        for conf in (None, "none", "low", "high"):
            engine, _ = self._held(confidence=conf)
            outcomes.append((engine._test_state.state, engine._last_comfort_hold["depth_f"]))
        assert len(set(outcomes)) == 1


class TestHoldLogPersistence:
    def _roundtrip(self, engine: AutomationEngine) -> AutomationEngine:
        state = json.loads(json.dumps(engine.get_serializable_state()))
        fresh = _make_engine(indoor=70.0, mode="off")
        fresh.restore_state(state)
        return fresh

    def _held(self):
        engine = _make_engine(indoor=67.0, mode="cool")
        clock = _Clock()
        _stamp_ran(engine, "cool", 40.0, clock.now)
        _apply(engine, clock, "mild", 67.0)
        return engine, clock

    def test_round_trip_keeps_closed_and_open_episodes(self):
        engine, clock = self._held()
        clock.advance(minutes=5)
        _apply(engine, clock, "mild", 71.0)  # closes the first
        clock.advance(minutes=11)
        _apply(engine, clock, "mild", 67.0)  # opens a second (beyond the merge window)
        fresh = self._roundtrip(engine)
        assert fresh.comfort_holds == engine.comfort_holds
        assert [h["end_reason"] for h in fresh.comfort_holds] == ["recovered", None]

    def test_an_open_episode_straddling_a_restart_is_continued_not_duplicated(self):
        engine, clock = self._held()
        fresh = self._roundtrip(engine)
        fresh._test_state.state = "cool"
        _stamp_ran(fresh, "cool", 40.0, clock.now)
        _apply(fresh, clock, "mild", 66.5)  # the same hold, after the restart
        assert len(fresh.comfort_holds) == 1
        assert fresh.comfort_holds[0]["max_depth_f"] == pytest.approx(1.5)
        clock.advance(minutes=5)
        _apply(fresh, clock, "mild", 71.0)
        assert fresh.comfort_holds[0]["end_reason"] == "recovered"

    def test_cap_is_enforced_when_logging_and_when_restoring(self):
        engine = _make_engine(indoor=67.0, mode="cool")
        engine._restore_comfort_holds(
            [
                {
                    "start_ts": (_T0 + timedelta(minutes=i)).isoformat(),
                    "end_ts": (_T0 + timedelta(minutes=i, seconds=30)).isoformat(),
                    "end_reason": "recovered",
                    "candidate_family": "heating",
                    "max_depth_f": 1.0,
                    "tolerance_f": 2.0,
                    "model_confidence": "low",
                }
                for i in range(COMFORT_HOLD_LOG_CAP + 25)
            ]
        )
        assert len(engine.comfort_holds) == COMFORT_HOLD_LOG_CAP
        assert engine.comfort_holds[-1]["start_ts"] == (_T0 + timedelta(minutes=COMFORT_HOLD_LOG_CAP + 24)).isoformat()
        clock = _Clock(_T0 + timedelta(days=1))
        _stamp_ran(engine, "cool", 40.0, clock.now)
        _apply(engine, clock, "mild", 67.0)  # one more episode
        assert len(engine.comfort_holds) == COMFORT_HOLD_LOG_CAP
        assert engine.comfort_holds[-1]["end_ts"] is None

    @pytest.mark.parametrize(
        "bad",
        [
            "not a list",
            [None, 3, "x", {}],
            [{"start_ts": "garbage", "candidate_family": "heating"}],
            [{"start_ts": _T0.isoformat(), "candidate_family": "sideways"}],
            [{"start_ts": _T0.isoformat(), "candidate_family": "heating", "end_ts": 5}],
        ],
    )
    def test_garbage_is_dropped_never_raised(self, bad):
        engine = _make_engine(indoor=67.0, mode="cool")
        engine._restore_comfort_holds(bad)
        assert engine.comfort_holds == []

    def test_types_are_validated_per_field(self):
        engine = _make_engine(indoor=67.0, mode="cool")
        engine._restore_comfort_holds(
            [
                {
                    "start_ts": _T0.isoformat(),
                    "end_ts": None,
                    "end_reason": "who-knows",
                    "candidate_family": "cooling",
                    "max_depth_f": "deep",
                    "tolerance_f": True,
                    "model_confidence": "extreme",
                }
            ]
        )
        h = engine.comfort_holds[0]
        assert h["end_reason"] is None and h["max_depth_f"] is None and h["tolerance_f"] is None
        assert h["model_confidence"] is None
