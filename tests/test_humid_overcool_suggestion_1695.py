"""Issue #1695 (plan #1507 wave 3b, I6): the "Turn on Humid Overcool" suggestion.

Occupant outcome: in a humid-climate house the AC keeps running while the indoor air is muggy (dew point at or above
the overcool trigger), but Humid Overcool is off (its default), so the short runs cool the room without drying it.
After enough humid cooling in the last two weeks the learning card says what turning it on would do. It is advice
only (#1652): "Got it" changes nothing, the option is never touched, and it is never shown while the option is on.
The minutes come only from reported cooling (``hvac_action`` cooling); a fan-only or idle humid day counts nothing.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from custom_components.climate_advisor import i18n
from custom_components.climate_advisor.const import CONF_HUMID_OVERCOOL_ENABLED
from custom_components.climate_advisor.coordinator import ClimateAdvisorCoordinator
from custom_components.climate_advisor.humidity_facts import OVERCOOL_HUMID_LATCH, SIGNAL_INDOOR, DewPoint
from custom_components.climate_advisor.learning import (
    HUMID_OVERCOOL_SUGGEST_MINUTES,
    SUGGESTION_KEYS,
    DailyRecord,
    LearningEngine,
)
from tests.test_hvac_session_detection import _make_state, _make_thermostat_coord, _make_thermostat_event

KEY = "humid_overcool"


def _rec(day: int, humid_minutes: float = 0.0, **fields) -> dict:
    return {
        "date": f"2026-07-{day:02d}",
        "day_type": "hot",
        "trend_direction": "stable",
        "occupancy_mode": "home",
        "hvac_runtime_minutes": 120.0,
        "hvac_cool_runtime_minutes": 120.0,
        "hvac_cool_humid_runtime_minutes": humid_minutes,
        **fields,
    }


def _engine(tmp_path: Path, *, enabled: bool | None = False, per_day: float = 0.0, days: int = 14) -> LearningEngine:
    probe = None if enabled is None else (lambda: enabled)
    engine = LearningEngine(tmp_path, humid_overcool_enabled=probe)
    engine._state.records.extend(_rec(i + 1, per_day) for i in range(days))
    return engine


def _keys(engine: LearningEngine) -> list[str]:
    engine.generate_suggestions()
    return engine.get_last_suggestion_keys()


# ── the suggestion ──────────────────────────────────────────────────────────────────────────────────────────────────
class TestSuggestion:
    def test_fires_with_the_option_off_and_enough_humid_cooling(self, tmp_path):
        # The #1507 reporter's kind of house: ~1.5 h of humid AC on a humid day, every day for two weeks.
        engine = _engine(tmp_path, per_day=90.0)
        assert KEY in _keys(engine)
        msg = engine.get_last_suggestion_msgs()[engine.get_last_suggestion_keys().index(KEY)]
        assert msg.key == "learning.suggestion.humid_overcool"
        assert msg.params["hours"] == 21  # 14 x 90 min
        text = i18n.render_en(msg)
        assert "Humid Overcool" in text and "off by default" in text and "Settings" in text
        assert "60°F" in text.replace(" ", "") and "2°F" in text.replace(" ", "")
        assert "Nothing changes unless you do" in text

    def test_celsius_home_reads_celsius(self, tmp_path):
        engine = LearningEngine(tmp_path, temp_unit="celsius", humid_overcool_enabled=lambda: False)
        engine._state.records.extend(_rec(i + 1, 30.0) for i in range(14))
        texts = engine.generate_suggestions()
        (text,) = [t for k, t in zip(engine.get_last_suggestion_keys(), texts, strict=True) if k == KEY]
        assert "°C" in text and "°F" not in text

    def test_never_with_the_option_on(self, tmp_path):
        assert KEY not in _keys(_engine(tmp_path, enabled=True, per_day=300.0))

    def test_never_when_the_option_is_unknown(self, tmp_path):
        assert KEY not in _keys(_engine(tmp_path, enabled=None, per_day=300.0))

    def test_never_when_the_option_cannot_be_read(self, tmp_path):
        def broken() -> bool:
            raise RuntimeError("config gone")

        engine = LearningEngine(tmp_path, humid_overcool_enabled=broken)
        engine._state.records.extend(_rec(i + 1, 300.0) for i in range(14))
        assert KEY not in _keys(engine)

    def test_reads_the_live_option(self, tmp_path):
        config = {CONF_HUMID_OVERCOOL_ENABLED: False}
        engine = LearningEngine(tmp_path, humid_overcool_enabled=lambda: bool(config[CONF_HUMID_OVERCOOL_ENABLED]))
        engine._state.records.extend(_rec(i + 1, 60.0) for i in range(14))
        assert KEY in _keys(engine)
        config[CONF_HUMID_OVERCOOL_ENABLED] = True  # the occupant turned it on
        assert KEY not in _keys(engine)

    def test_threshold_boundary(self, tmp_path):
        below = _engine(tmp_path, days=14)
        below._state.records[-1]["hvac_cool_humid_runtime_minutes"] = HUMID_OVERCOOL_SUGGEST_MINUTES - 1.0
        assert KEY not in _keys(below)
        below._state.records[-1]["hvac_cool_humid_runtime_minutes"] = HUMID_OVERCOOL_SUGGEST_MINUTES
        assert KEY in _keys(below)

    def test_only_the_last_14_days_count(self, tmp_path):
        engine = LearningEngine(tmp_path, humid_overcool_enabled=lambda: False)
        engine._state.records.extend(_rec(i + 1, 600.0) for i in range(5))  # an old humid spell
        engine._state.records.extend(_rec(i + 6, 0.0) for i in range(14))  # two dry weeks since
        assert KEY not in _keys(engine)

    def test_plain_cooling_without_humid_minutes_does_not_fire(self, tmp_path):
        # plenty of AC, none of it with the house at the trigger (the owner's dry-summer house on most days)
        assert KEY not in _keys(_engine(tmp_path, per_day=0.0))

    def test_vacation_days_do_not_count(self, tmp_path):
        engine = LearningEngine(tmp_path, humid_overcool_enabled=lambda: False)
        engine._state.records.extend(_rec(i + 1, 100.0, occupancy_mode="vacation") for i in range(14))
        assert KEY not in _keys(engine)

    def test_dismissed_stays_dismissed(self, tmp_path):
        engine = _engine(tmp_path, per_day=90.0)
        engine.dismiss_suggestion(KEY)
        assert KEY not in _keys(engine)

    def test_got_it_changes_nothing_and_holds_it_back(self, tmp_path):
        config = {CONF_HUMID_OVERCOOL_ENABLED: False}
        engine = LearningEngine(tmp_path, humid_overcool_enabled=lambda: bool(config[CONF_HUMID_OVERCOOL_ENABLED]))
        engine._state.records.extend(_rec(i + 1, 90.0) for i in range(14))
        assert KEY in SUGGESTION_KEYS
        assert engine.accept_suggestion(KEY) == {}
        assert config == {CONF_HUMID_OVERCOOL_ENABLED: False}  # the option is never changed by learning (#1652)
        assert engine._state.settings_history[-1] == {
            "timestamp": engine._state.settings_history[-1]["timestamp"],
            "suggestion": KEY,
            "changes": {},
        }
        assert KEY not in _keys(engine)  # held back 30 days

    def test_old_records_without_the_field_load(self, tmp_path):
        engine = LearningEngine(tmp_path, humid_overcool_enabled=lambda: False)
        engine.save_state()
        path = engine._db_path
        data = json.loads(path.read_text())
        old = []
        for i in range(14):
            r = _rec(i + 1)
            del r["hvac_cool_humid_runtime_minutes"]  # a record stored before 0.9.80
            old.append(r)
        data["records"] = old
        path.write_text(json.dumps(data))
        loaded = LearningEngine(tmp_path, humid_overcool_enabled=lambda: False)
        loaded.load_state()
        assert len(loaded._state.records) == 14
        assert KEY not in _keys(loaded)  # no humid minutes recorded: nothing to suggest, no crash
        # the coordinator's restore path rebuilds today's record from such a dict
        assert DailyRecord(**old[0]).hvac_cool_humid_runtime_minutes == 0.0
        # once new records carry the field they count
        loaded._state.records.extend(_rec(i + 15, 120.0) for i in range(2))
        assert KEY in _keys(loaded)


# ── the minutes: reported cooling only, split by the verdict humid overcool engages on ─────────────────────────────
def _indoor_dp(value: float | None):
    """A ``_read_dew_point`` stub: the indoor signal reads ``value`` (None = unknown), the outdoor one is unknown."""

    def reading(signal, _now):
        if signal != SIGNAL_INDOOR or value is None:
            return DewPoint(None, reason="no_source")
        return DewPoint(value, rh=60.0, temp_f=76.0, humidity_source="sensor.rh", temp_source="sensor.t")

    return reading


def _flush_coord(latch_on: bool, dp: float | None = None) -> ClimateAdvisorCoordinator:
    """A coordinator whose tick left the overcool latch ``latch_on`` and whose indoor dew point reads ``dp`` now
    (default: 62 °F when the latch is on, 55 °F when off)."""
    coord = object.__new__(ClimateAdvisorCoordinator)
    coord.config = {}
    coord._today_record = DailyRecord(date="2026-07-12", day_type="hot", trend_direction="stable")
    coord._humid_latch_state = {OVERCOOL_HUMID_LATCH: latch_on}
    coord._dew_point_history = None
    coord._read_dew_point = _indoor_dp((62.0 if latch_on else 55.0) if dp is None else dp)
    return coord


class TestMinutes:
    @pytest.mark.parametrize(
        ("latch_on", "dp", "humid"),
        [
            (True, None, 20.0),
            (False, None, 0.0),
            # The overcool latch's own hysteresis (on at 60 °F, off at or below 58 °F), read through
            # humidity_facts.overcool_humid_from_facts exactly as the overcool decision reads it:
            (True, 59.0, 20.0),  # still humid: held above the 58 °F release
            (False, 59.0, 0.0),  # not yet humid: below the 60 °F trigger
            (False, 60.0, 20.0),  # the decision would engage now, so the slice is humid
            (True, 58.0, 0.0),  # released at 58 °F: the tick's stale "on" must not count
            (True, "unknown", 0.0),  # an unknown dew point releases the latch
        ],
    )
    def test_cool_flush_splits_by_the_overcool_verdict(self, latch_on, dp, humid):
        coord = _flush_coord(latch_on, None if dp == "unknown" else dp)
        if dp == "unknown":
            coord._read_dew_point = _indoor_dp(None)
        start = datetime(2026, 7, 12, 14, 0)
        coord._hvac_on_since = start
        with patch("custom_components.climate_advisor.coordinator.dt_util") as dt:
            dt.now.return_value = start + timedelta(minutes=20)
            coord._flush_runtime_for_mode("cool")
        rec = coord._today_record
        assert rec.hvac_cool_runtime_minutes == pytest.approx(20.0)
        assert rec.hvac_cool_humid_runtime_minutes == pytest.approx(humid)

    @pytest.mark.parametrize("mode", ["heat", "fan_only"])
    def test_heat_and_fan_only_never_count_as_humid_cooling(self, mode):
        coord = _flush_coord(True)
        start = datetime(2026, 7, 12, 14, 0)
        coord._hvac_on_since = start
        coord._thermostat_fan_only_on_since = start
        with patch("custom_components.climate_advisor.coordinator.dt_util") as dt:
            dt.now.return_value = start + timedelta(minutes=30)
            coord._flush_runtime_for_mode(mode)
        assert coord._today_record.hvac_cool_humid_runtime_minutes == 0.0

    @pytest.mark.parametrize(("action", "humid"), [("cooling", 30.0), ("fan", 0.0), ("idle", 0.0)])
    def test_only_reported_cooling_counts(self, action, humid):
        """A humid day with the thermostat in cool mode but reporting fan or idle is not a cooling run."""
        coord = _make_thermostat_coord()
        coord._humid_latch_state = {OVERCOOL_HUMID_LATCH: True}
        coord._dew_point_history = None
        coord._read_dew_point = _indoor_dp(62.0)
        coord._cycle_thermostat_event = MagicMock()
        with patch("custom_components.climate_advisor.coordinator.dt_util") as dt:
            dt.now.return_value = datetime(2026, 7, 12, 14, 0)
            asyncio.run(
                coord._async_thermostat_changed(
                    _make_thermostat_event(_make_state("cool", "idle"), _make_state("cool", action))
                )
            )
            dt.now.return_value = datetime(2026, 7, 12, 14, 30)
            asyncio.run(
                coord._async_thermostat_changed(
                    _make_thermostat_event(_make_state("cool", action), _make_state("cool", "idle"))
                )
            )
        rec = coord._today_record
        assert rec.hvac_cool_humid_runtime_minutes == pytest.approx(humid)
        assert rec.hvac_cool_humid_runtime_minutes <= rec.hvac_cool_runtime_minutes

    def test_the_five_minute_tick_credits_each_slice_under_its_own_verdict(self):
        """A run that starts dry and turns muggy at 14:20: only the slices the overcool decision would call humid
        count. Ticks every 5 minutes; the 15-minute median holds the verdict dry until two muggy samples are in."""
        coord = _flush_coord(False)
        coord.hass = MagicMock()
        coord.automation_engine = MagicMock()
        coord._reconnect_watch_for = MagicMock()
        coord._humidity_role_inputs = MagicMock(return_value=(None, [], None))
        dp = {"value": 55.0}
        coord._read_dew_point = lambda signal, now: _indoor_dp(dp["value"])(signal, now)
        start = datetime(2026, 7, 12, 14, 0)
        coord._hvac_session_mode = "cool"
        coord._hvac_on_since = start
        latch_after: dict[int, bool] = {}
        with patch("custom_components.climate_advisor.coordinator.dt_util") as dt:
            for minute in range(5, 65, 5):
                tick = start + timedelta(minutes=minute)
                dp["value"] = 62.0 if minute >= 20 else 55.0  # the house turned muggy at 14:20
                dt.now.return_value = tick
                coord._sample_humidity_facts(tick)
                latch_after[minute] = coord._humid_latch_state[OVERCOOL_HUMID_LATCH]
            dt.now.return_value = start + timedelta(minutes=62)
            coord._flush_runtime_for_mode("cool")  # the run ends: 2 more humid minutes
        assert latch_after[20] is False and latch_after[25] is True  # median: [55, 55, 62, 62, 62] at 14:25
        rec = coord._today_record
        assert rec.hvac_cool_runtime_minutes == pytest.approx(62.0)
        # 14:00-14:25 dry (the 14:20 and 14:25 medians are 55 and 58.5), 14:25-15:02 humid = 7 x 5 + 2 minutes.
        assert rec.hvac_cool_humid_runtime_minutes == pytest.approx(37.0)

    def test_tick_without_a_cooling_session_credits_nothing(self):
        coord = _flush_coord(True)
        coord.hass = MagicMock()
        coord.automation_engine = MagicMock()
        coord._dew_point_history = None
        coord._reconnect_watch_for = MagicMock()
        coord._humidity_role_inputs = MagicMock(return_value=(None, [], None))
        coord._read_dew_point = lambda _s, _n: DewPoint(63.0, rh=65.0, temp_f=76.0, humidity_source="sensor.rh")
        coord._hvac_session_mode = "fan_only"  # a humid fan-only afternoon
        coord._hvac_on_since = None
        coord._thermostat_fan_only_on_since = datetime(2026, 7, 12, 14, 0)
        coord._sample_humidity_facts(datetime(2026, 7, 12, 15, 0))
        assert coord._today_record.hvac_cool_humid_runtime_minutes == 0.0
        assert coord._today_record.hvac_cool_runtime_minutes == 0.0


def test_the_coordinator_wires_the_live_option():
    """The real coordinator hands learning a reader of its own live config, not a copy taken at setup."""
    from tools.sim_harness.build_coordinator import build_headless_multi_zone

    zones, _hass, _scheduler = build_headless_multi_zone(configs=[{"climate_entity": "climate.zone_a"}])
    coord = zones["zone_0"]["coordinator"]
    probe = coord.learning._humid_overcool_enabled
    assert probe is not None and probe() is False  # off by default
    coord.config[CONF_HUMID_OVERCOOL_ENABLED] = True
    assert probe() is True
