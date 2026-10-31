"""Issue #1073 (S6 / #1070): restores and override adoption use the RESOLVED band through the selector.

Occupant framing: a door closes at 23:00 while the house is on its night band (sleep 64/70). Before
this change the restore wrote the flat DAYTIME band (68/74) -- the occupant woke up a degree or two off
their night setpoint for a few minutes after every door, resume or nat-vent exit; on an aggressive-savings
day the restore ignored the widened band; and a restore bypassed the settling guard. A user who set cool
on a warm day (classification mode "off") was never recognised as matching the automation's own
decision. All restores now end at ``_set_temperature_for_mode`` which asks the selector on the SAME band
``_apply_comfort_band`` arms and writes that band's edge.

Real ``AutomationEngine`` + a fake thermostat that follows the engine's commands. Nothing here
re-implements production logic.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.climate_advisor.automation import AutomationEngine
from custom_components.climate_advisor.classifier import DayClassification
from custom_components.climate_advisor.const import COMFORT_RECENT_COMMAND_WINDOW_S

_AUTOMATION = "custom_components.climate_advisor.automation"
_NIGHT = datetime(2026, 10, 11, 23, 0, 0, tzinfo=UTC)
_DAY = datetime(2026, 10, 11, 14, 0, 0, tzinfo=UTC)
_BOTH = ["off", "heat", "cool"]
_HVAC_MODE_FOR_DAY = {"hot": "cool", "warm": "off", "mild": "off", "cool": "off", "cold": "heat"}
_SLEEP_CFG = {"sleep_time": "22:00", "wake_time": "07:00", "sleep_heat": 64.0, "sleep_cool": 70.0}


def _consume_coroutine(coro):
    coro.close()


def _run(coro):
    return asyncio.run(coro)


def _make_engine(*, indoor: float, mode: str = "off", **cfg) -> AutomationEngine:
    hass = MagicMock()
    hass.async_create_task = MagicMock(side_effect=_consume_coroutine)
    hass.states = MagicMock()
    state = MagicMock()
    state.state = mode
    state.attributes = {"hvac_modes": list(_BOTH), "supported_features": 1, "current_temperature": indoor}
    hass.states.get.return_value = state

    async def _call(domain, service, data, **_kw):
        if domain != "climate":
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
    engine._thermal_model = {}
    engine._test_state = state
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


def _setpoints(engine: AutomationEngine) -> list[tuple[str | None, float]]:
    return [
        (c.args[2].get("hvac_mode"), c.args[2]["temperature"])
        for c in engine.hass.services.async_call.call_args_list
        if c.args[0] == "climate" and c.args[1] == "set_temperature"
    ]


def _restore(engine: AutomationEngine, day: str, now: datetime, **kw) -> None:
    c = _classification(day)
    engine._current_classification = c
    with patch(f"{_AUTOMATION}.dt_util.now", return_value=now):
        _run(engine._set_temperature_for_mode(c, reason="test restore", **kw))


def _stamp_ran(engine: AutomationEngine, direction: str, ago_min: float, now: datetime) -> None:
    """Record the opposite family's last run through the engine's persisted field the selector reads."""
    iso = (now - timedelta(minutes=ago_min)).isoformat()
    if direction == "heat":
        engine._last_hvac_heating_active = iso
    else:
        engine._last_hvac_cooling_active = iso


# ---------------------------------------------------------------------------
# The choke point: every restore resolves on the band apply_classification() arms
# ---------------------------------------------------------------------------


class TestRestoreOnResolvedBand:
    def test_sleep_band_cool_restore_writes_the_sleep_ceiling(self):
        engine = _make_engine(indoor=71.0, **_SLEEP_CFG)
        _restore(engine, "hot", _NIGHT)
        assert _setpoints(engine)[-1] == ("cool", 70.0), "night restore must use sleep_cool, not comfort_cool 74"

    def test_sleep_band_heat_restore_writes_the_sleep_floor(self):
        engine = _make_engine(indoor=65.0, **_SLEEP_CFG)
        _restore(engine, "cold", _NIGHT)
        assert _setpoints(engine)[-1] == ("heat", 64.0), "night restore must use sleep_heat, not comfort_heat 68"

    def test_daytime_restore_is_unchanged_on_the_comfort_band(self):
        hot = _make_engine(indoor=73.0, **_SLEEP_CFG)
        _restore(hot, "hot", _DAY)
        assert _setpoints(hot)[-1] == ("cool", 74.0)
        cold = _make_engine(indoor=69.0, **_SLEEP_CFG)
        _restore(cold, "cold", _DAY)
        assert _setpoints(cold)[-1] == ("heat", 68.0)

    def test_aggressive_savings_cool_restore_uses_the_widened_ceiling(self):
        engine = _make_engine(indoor=75.0, aggressive_savings=True)
        _restore(engine, "hot", _DAY)
        assert _setpoints(engine)[-1] == ("cool", 76.0)

    def test_aggressive_savings_heat_restore_uses_the_widened_floor(self):
        engine = _make_engine(indoor=67.0, aggressive_savings=True)
        _restore(engine, "cold", _DAY)
        assert _setpoints(engine)[-1] == ("heat", 66.0)

    @pytest.mark.parametrize("occupancy", ["away", "vacation"])
    def test_away_and_vacation_still_redirect_to_the_setback_handlers(self, occupancy):
        engine = _make_engine(indoor=71.0)
        engine._occupancy_mode = occupancy
        engine.handle_occupancy_away = AsyncMock()
        engine.handle_occupancy_vacation = AsyncMock()
        _restore(engine, "hot", _DAY)
        assert (engine.handle_occupancy_away if occupancy == "away" else engine.handle_occupancy_vacation).await_count
        assert _setpoints(engine) == [], "a redirected restore writes no comfort setpoint"

    def test_tou_explicit_target_bypasses_the_selector_and_the_band(self):
        engine = _make_engine(indoor=71.0, **_SLEEP_CFG)
        _restore(engine, "hot", _NIGHT, target_override=60.0, skip_setpoint_sanity_check=True)
        assert _setpoints(engine)[-1] == ("cool", 60.0), "TOU banking target must be written as given"

    def test_resolver_fault_degrades_to_the_flat_band_not_a_skipped_restore(self):
        engine = _make_engine(indoor=73.0)
        with patch(f"{_AUTOMATION}.select_comfort_band", side_effect=RuntimeError("boom")):
            _restore(engine, "hot", _DAY)
        assert _setpoints(engine)[-1] == ("cool", 74.0)

    def test_restore_is_logged_at_info_with_the_resolved_band(self, caplog):
        engine = _make_engine(indoor=71.0, **_SLEEP_CFG)
        with caplog.at_level("INFO", logger=_AUTOMATION):
            _restore(engine, "hot", _NIGHT)
        msgs = [r.getMessage() for r in caplog.records if "Restore applied via selector" in r.getMessage()]
        assert len(msgs) == 1
        assert "resolved_mode=cool" in msgs[0]
        assert "band_changed_setpoint=True" in msgs[0]


class TestRestoreGoesThroughTheSelectorAndSettlingGuard:
    """R6: the settling guard applies to restores too -- a restore can now HOLD instead of flip."""

    def test_cool_restore_holds_a_shallow_floor_undershoot_after_cooling_ran(self):
        engine = _make_engine(indoor=67.0, mode="cool")  # depth 1 under the 68 floor, cool just ran
        _stamp_ran(engine, "cool", 40.0, _DAY)
        _restore(engine, "hot", _DAY)
        assert _setpoints(engine)[-1] == ("cool", 74.0), "must keep cooling, not flip to heat"
        assert engine._last_comfort_hold is not None

    def test_heat_restore_holds_a_shallow_ceiling_overshoot_after_heating_ran(self):
        engine = _make_engine(indoor=75.0, mode="heat")  # mirror
        _stamp_ran(engine, "heat", 40.0, _DAY)
        _restore(engine, "cold", _DAY)
        assert _setpoints(engine)[-1] == ("heat", 68.0)
        assert engine._last_comfort_hold is not None

    def test_deep_breach_restore_still_switches_at_once(self):
        engine = _make_engine(indoor=62.0, mode="cool")  # depth 6 > hot-day tolerance 5
        _stamp_ran(engine, "cool", 5.0, _DAY)
        _restore(engine, "hot", _DAY)
        assert _setpoints(engine)[-1] == ("heat", 68.0)

    def test_hold_on_the_sleep_band_uses_the_sleep_edges(self):
        engine = _make_engine(indoor=63.0, mode="cool", **_SLEEP_CFG)  # depth 1 under sleep_heat 64
        _stamp_ran(engine, "cool", 40.0, _NIGHT)
        _restore(engine, "hot", _NIGHT)
        assert _setpoints(engine)[-1] == ("cool", 70.0)
        assert engine._last_comfort_hold["floor_f"] == 64.0


# ---------------------------------------------------------------------------
# Real callers of the choke point
# ---------------------------------------------------------------------------


class TestRealRestoreCallers:
    def test_door_window_close_restores_the_night_band(self):
        engine = _make_engine(indoor=71.0, **_SLEEP_CFG)
        engine._current_classification = _classification("hot")
        engine._paused_by_door = True
        engine._pre_pause_mode = "cool"
        with patch(f"{_AUTOMATION}.dt_util.now", return_value=_NIGHT):
            _run(engine.handle_all_doors_windows_closed())
        assert _setpoints(engine)[-1] == ("cool", 70.0)

    def test_door_window_close_restores_the_widened_aggressive_band(self):
        engine = _make_engine(indoor=67.0, aggressive_savings=True)
        engine._current_classification = _classification("cold")
        engine._paused_by_door = True
        engine._pre_pause_mode = "heat"
        with patch(f"{_AUTOMATION}.dt_util.now", return_value=_DAY):
            _run(engine.handle_all_doors_windows_closed())
        assert _setpoints(engine)[-1] == ("heat", 66.0)

    def test_resume_from_pause_restores_the_night_band(self):
        engine = _make_engine(indoor=65.0, **_SLEEP_CFG)
        engine._current_classification = _classification("cold")
        engine._paused_by_door = True
        engine._pre_pause_mode = "heat"
        with patch(f"{_AUTOMATION}.dt_util.now", return_value=_NIGHT):
            restored = _run(engine.resume_from_pause())
        assert restored == "heat"
        assert _setpoints(engine)[-1] == ("heat", 64.0)

    def test_economizer_off_restores_the_night_band(self):
        engine = _make_engine(indoor=71.0, **_SLEEP_CFG)
        engine._current_classification = _classification("hot")
        engine._economizer_active = True
        with patch(f"{_AUTOMATION}.dt_util.now", return_value=_NIGHT):
            _run(engine._deactivate_economizer(60.0))
        assert _setpoints(engine)[-1] == ("cool", 70.0)

    def test_nat_vent_exit_restores_the_night_band(self):
        engine = _make_engine(indoor=71.0, **_SLEEP_CFG)
        engine._current_classification = _classification("hot")
        engine._natural_vent_active = True
        with patch(f"{_AUTOMATION}.dt_util.now", return_value=_NIGHT):
            _run(engine._exit_nat_vent(reason="outdoor rose"))
        assert _setpoints(engine)[-1] == ("cool", 70.0)

    def test_occupancy_home_restores_the_resolved_band(self):
        engine = _make_engine(indoor=75.0, aggressive_savings=True)
        engine._current_classification = _classification("hot")
        engine._occupancy_mode = "home"
        with patch(f"{_AUTOMATION}.dt_util.now", return_value=_DAY):
            _run(engine.handle_occupancy_home())
        assert _setpoints(engine), "arrival-home restore wrote nothing"
        assert _setpoints(engine)[-1] == ("cool", 76.0)


# ---------------------------------------------------------------------------
# Override adoption compares to the selector family and the resolved edge
# ---------------------------------------------------------------------------


def _override_engine(*, day: str, indoor: float, user_mode: str, setpoint: float, source: str = "normal", **cfg):
    engine = _make_engine(indoor=indoor, mode=user_mode, **cfg)
    engine._test_state.attributes["temperature"] = setpoint
    engine._manual_override_active = True
    engine._manual_override_mode = user_mode
    engine._manual_override_source = source
    return engine, _classification(day)


def _matches(engine, classification, now=_DAY) -> bool:
    with patch(f"{_AUTOMATION}.dt_util.now", return_value=now):
        return engine._override_matches_current_decision(classification)


class TestOverrideAdoptionOnResolvedBand:
    def test_warm_day_cool_override_at_the_ceiling_is_adopted(self):
        # warm day: classification.hvac_mode == "off", yet the selector arms cool near the ceiling.
        engine, c = _override_engine(day="warm", indoor=73.0, user_mode="cool", setpoint=74.0)
        assert c.hvac_mode == "off"
        assert _matches(engine, c) is True

    def test_mild_day_heat_override_at_the_floor_is_adopted(self):  # heat mirror
        engine, c = _override_engine(day="mild", indoor=69.0, user_mode="heat", setpoint=68.0)
        assert c.hvac_mode == "off"
        assert _matches(engine, c) is True

    def test_override_with_a_different_setpoint_is_not_adopted(self):
        engine, c = _override_engine(day="warm", indoor=73.0, user_mode="cool", setpoint=70.0)
        assert _matches(engine, c) is False

    def test_override_to_the_family_the_selector_would_not_arm_is_not_adopted(self):
        # indoor 69.5 is in the heat zone (<= floor+2) -> the selector arms heat; the user chose cool
        engine, c = _override_engine(day="mild", indoor=69.5, user_mode="cool", setpoint=74.0, **{})
        engine._test_state.state = "heat"  # thermostat memory is heat, indoor is near the floor
        assert _matches(engine, c) is False

    def test_sleep_band_adoption_compares_to_the_sleep_edge(self):
        near = _override_engine(day="hot", indoor=70.0, user_mode="cool", setpoint=70.0, **_SLEEP_CFG)
        assert _matches(*near, now=_NIGHT) is True
        flat = _override_engine(day="hot", indoor=70.0, user_mode="cool", setpoint=74.0, **_SLEEP_CFG)
        assert _matches(*flat, now=_NIGHT) is False, "the daytime ceiling is NOT the night decision"

    def test_aggressive_savings_adoption_compares_to_the_widened_edge(self):
        engine, c = _override_engine(day="hot", indoor=75.0, user_mode="cool", setpoint=76.0, aggressive_savings=True)
        assert _matches(engine, c) is True

    def test_setpoint_only_override_never_adopts(self):
        engine, c = _override_engine(day="hot", indoor=73.0, user_mode="cool", setpoint=74.0, source="setpoint")
        assert _matches(engine, c) is False

    def test_no_override_or_no_classification_is_not_a_match(self):
        engine, c = _override_engine(day="hot", indoor=73.0, user_mode="cool", setpoint=74.0)
        assert _matches(engine, None) is False
        engine._manual_override_active = False
        assert _matches(engine, c) is False

    def test_off_override_matches_only_while_ca_holds_hvac_off(self):
        # Issue #1159: on a warm/mild day the "off" label is not a plan to hold HVAC off (CA arms the
        # band), so the occupant's Off is a real override and is NOT adopted ...
        engine, c = _override_engine(day="warm", indoor=71.0, user_mode="off", setpoint=70.0)
        assert _matches(engine, c) is False
        # ... unless CA has actually paused HVAC for a door/window (_paused_by_door), where Off agrees with CA.
        engine._paused_by_door = True
        assert _matches(engine, c) is True
        hot_engine, hot = _override_engine(day="hot", indoor=71.0, user_mode="off", setpoint=70.0)
        assert _matches(hot_engine, hot) is False

    def test_adoption_preview_does_not_mutate_selector_state(self):
        engine, c = _override_engine(day="warm", indoor=73.0, user_mode="cool", setpoint=74.0)
        before = (engine._last_resolved_family, engine._comfort_hold_signature, list(engine._comfort_holds))
        _matches(engine, c)
        assert (engine._last_resolved_family, engine._comfort_hold_signature, list(engine._comfort_holds)) == before


def test_recently_commanded_window_is_a_named_constant():
    """R9: the 30 s 'a command we just sent beats the lagging entity state' window has a const row."""
    assert COMFORT_RECENT_COMMAND_WINDOW_S == 30.0
    engine = _make_engine(indoor=70.0, mode="off")
    engine._last_commanded_hvac_mode = "cool"
    engine._last_commanded_hvac_time = _DAY
    inside = engine._current_thermostat_family(_DAY + timedelta(seconds=COMFORT_RECENT_COMMAND_WINDOW_S - 1))
    outside = engine._current_thermostat_family(_DAY + timedelta(seconds=COMFORT_RECENT_COMMAND_WINDOW_S + 1))
    assert inside is not None and inside.value == "cooling"
    assert outside is None, "past the window the live (Off) state is the memory"
