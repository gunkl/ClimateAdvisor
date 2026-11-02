"""Issue #1258: no comfort incident / violation minute while the room is only inside the thermostat's own swing.

Occupant experience being guarded: the thermostat is in heat and, by its own configuration, lets the room drift
about its learned heat swing below the setpoint before it fires (67 F against a 68 F floor with a 1.4 F swing).
The house is doing exactly what the occupant set it to do, yet the Activity Log said "Incident detected:
comfort_undertemp", the comfort score dropped and the occupant was told something was wrong. The real alarms stay:
a room beyond the swing, or a cold room with the thermostat Off / in the opposite mode.

Written strict-xfail first against the unchanged coordinator incident scan (17 failed for the right reason: an
incident / 30 violation minutes the occupant should never have seen, or a missing payload key); the cases that
already behaved correctly are regression guards. The pure rule is tested directly in the last class.
"""

from __future__ import annotations

import types
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from custom_components.climate_advisor.swing import (
    SWING_SOURCE_DEFAULT,
    SWING_SOURCE_LEARNED,
    swing_tolerance,
    thermostat_holds_direction,
    within_thermostat_hysteresis,
)
from tests.test_incident_detection import _make_coordinator_stub
from tools.sim_harness._loop import run_coro
from tools.sim_harness.build_coordinator import build_headless_coordinator
from tools.sim_harness.fake_hass import FakeState

_HYSTERESIS_BINDINGS = (
    "_learned_swings",
    "_thermostat_hvac_mode",
    "_is_within_thermostat_hysteresis",
    "_is_tolerated_comfort_deviation",
    "_hysteresis_incident_context",
)


def _coord(*, mode: str, swing_heat=1.4, swing_cool=1.4, nat_vent=False, heat=68.0, cool=74.0):
    """The incident-test coordinator stub with the thermostat in *mode* and the learned swings set."""
    from custom_components.climate_advisor.coordinator import ClimateAdvisorCoordinator

    coord = _make_coordinator_stub({"comfort_heat": heat, "comfort_cool": cool})
    for name in _HYSTERESIS_BINDINGS:  # before the fix these do not exist (strict xfail proves the right reason)
        fn = getattr(ClimateAdvisorCoordinator, name, None)
        if fn is not None:
            setattr(coord, name, types.MethodType(fn, coord))
    coord.automation_engine.climate_entity = "climate.test"
    coord.automation_engine._natural_vent_active = nat_vent
    coord.hass.states.get.return_value = SimpleNamespace(state=mode, attributes={"hvac_action": "idle"})
    coord.learning.get_thermal_model.return_value = {"swing_heat_f": swing_heat, "swing_cool_f": swing_cool}
    return coord


def _scan(coord, indoor: float) -> list[dict]:
    coord._get_indoor_temp.return_value = indoor
    with patch("custom_components.climate_advisor.coordinator.dt_util") as mock_dt:
        mock_dt.now.return_value = datetime.now(UTC)
        coord._detect_and_emit_incidents()
    return [e for e in coord._event_log if e.get("incident_class") in ("comfort_undertemp", "comfort_violation")]


class TestUnderTemperatureHeatSide:
    @pytest.mark.parametrize("swing", [1.5, 1.4])
    def test_the_clip_heat_idle_67_vs_68_is_not_an_incident(self, swing: float) -> None:
        assert _scan(_coord(mode="heat", swing_heat=swing), 67.0) == []

    def test_beyond_the_swing_is_still_an_incident(self) -> None:
        inc = _scan(_coord(mode="heat", swing_heat=1.4), 66.3)
        assert [e["incident_class"] for e in inc] == ["comfort_undertemp"]

    def test_exactly_one_swing_out_is_tolerated(self) -> None:
        assert _scan(_coord(mode="heat", swing_heat=1.4), 66.6) == []

    def test_unlearned_swing_uses_the_1_5_default(self) -> None:
        assert _scan(_coord(mode="heat", swing_heat=None), 66.6) == []  # 1.4 out < 1.5
        assert [e["incident_class"] for e in _scan(_coord(mode="heat", swing_heat=None), 66.4)] == ["comfort_undertemp"]

    def test_a_tiny_learned_swing_is_floored_at_half_a_degree(self) -> None:
        # swing 0.3 -> tolerance 0.5: 0.5 under is still not an incident (it never was), 0.6 under is.
        assert _scan(_coord(mode="heat", swing_heat=0.3), 67.5) == []
        assert [e["incident_class"] for e in _scan(_coord(mode="heat", swing_heat=0.3), 67.4)] == ["comfort_undertemp"]

    def test_thermostat_off_keeps_the_old_rule(self) -> None:
        inc = _scan(_coord(mode="off", swing_heat=1.4), 67.0)
        assert [e["incident_class"] for e in inc] == ["comfort_undertemp"]

    def test_thermostat_in_cool_while_cold_keeps_the_old_rule(self) -> None:
        inc = _scan(_coord(mode="cool", swing_heat=1.4), 67.0)
        assert [e["incident_class"] for e in inc] == ["comfort_undertemp"]

    def test_unknown_thermostat_state_keeps_the_old_rule(self) -> None:
        inc = _scan(_coord(mode="unavailable", swing_heat=1.4), 67.0)
        assert [e["incident_class"] for e in inc] == ["comfort_undertemp"]

    @pytest.mark.parametrize("mode", ["heat_cool", "auto"])
    def test_heat_cool_and_auto_hold_the_heat_side(self, mode: str) -> None:
        assert _scan(_coord(mode=mode, swing_heat=1.4), 67.0) == []

    def test_within_half_a_degree_is_never_an_incident(self) -> None:
        assert _scan(_coord(mode="off", swing_heat=1.4), 67.6) == []


class TestOverTemperatureCoolSide:
    @pytest.mark.parametrize("swing", [1.5, 1.4])
    def test_cool_idle_just_above_the_ceiling_is_not_a_violation(self, swing: float) -> None:
        assert _scan(_coord(mode="cool", swing_cool=swing), 75.0) == []

    def test_beyond_the_swing_is_still_a_violation(self) -> None:
        inc = _scan(_coord(mode="cool", swing_cool=1.4), 75.6)
        assert [e["incident_class"] for e in inc] == ["comfort_violation"]

    def test_unlearned_swing_uses_the_default(self) -> None:
        assert _scan(_coord(mode="cool", swing_cool=None), 75.4) == []

    def test_a_tiny_learned_swing_is_floored(self) -> None:
        assert _scan(_coord(mode="cool", swing_cool=0.3), 74.5) == []
        assert [e["incident_class"] for e in _scan(_coord(mode="cool", swing_cool=0.3), 74.6)] == ["comfort_violation"]

    def test_thermostat_off_keeps_the_old_rule(self) -> None:
        inc = _scan(_coord(mode="off", swing_cool=1.4), 75.0)
        assert [e["incident_class"] for e in inc] == ["comfort_violation"]

    def test_thermostat_in_heat_while_hot_keeps_the_old_rule(self) -> None:
        inc = _scan(_coord(mode="heat", swing_cool=1.4), 75.0)
        assert [e["incident_class"] for e in inc] == ["comfort_violation"]

    @pytest.mark.parametrize("mode", ["heat_cool", "auto"])
    def test_heat_cool_and_auto_hold_the_cool_side(self, mode: str) -> None:
        assert _scan(_coord(mode=mode, swing_cool=1.4), 75.0) == []


class TestComposesWithNatVentTolerance:
    def test_nat_vent_tolerance_still_works_with_the_thermostat_off(self) -> None:
        # nat-vent hysteresis is 1.0 F: 74.9 is tolerated by nat-vent alone (thermostat Off, so no swing tolerance).
        assert _scan(_coord(mode="off", nat_vent=True), 74.9) == []

    def test_nat_vent_does_not_hide_a_room_beyond_both(self) -> None:
        inc = _scan(_coord(mode="heat", swing_cool=1.4, nat_vent=True), 80.0)
        assert [e["incident_class"] for e in inc] == ["comfort_violation"]

    def test_either_tolerance_suffices(self) -> None:
        # 67.0 vs 68: nat-vent (1.0) tolerates it while swing 0.2 -> floor 0.5 does not; the thermostat is in heat
        # with a 1.4 swing -> the swing tolerates it with nat-vent off. Both paths already agree on "no incident"
        # only after the fix, so the swing path is the xfail.
        assert _scan(_coord(mode="heat", swing_heat=1.4, nat_vent=False), 67.0) == []
        assert _scan(_coord(mode="heat", swing_heat=0.2, nat_vent=True), 67.0) == []


class TestPayloadCarriesTheTolerance:
    def test_a_beyond_swing_undertemp_incident_records_the_learned_tolerance(self) -> None:
        inc = _scan(_coord(mode="heat", swing_heat=1.4), 66.0)
        assert inc[0]["swing_tolerance_f"] == 1.4
        assert inc[0]["swing_tolerance_source"] == SWING_SOURCE_LEARNED
        assert inc[0]["swing_tolerance_applied"] is True

    def test_the_default_source_is_recorded(self) -> None:
        inc = _scan(_coord(mode="cool", swing_cool=None), 77.0)
        assert inc[0]["swing_tolerance_f"] == 1.5
        assert inc[0]["swing_tolerance_source"] == SWING_SOURCE_DEFAULT

    def test_thermostat_off_records_that_no_swing_tolerance_applied(self) -> None:
        inc = _scan(_coord(mode="off", swing_heat=1.4), 66.0)
        assert inc[0]["swing_tolerance_applied"] is False
        assert inc[0]["swing_tolerance_f"] == 1.4


# ---- the comfort_violations_minutes mirror (feeds comfort_score) -----------------------------------------------

_DAY = datetime(2024, 1, 15, 14, 0, tzinfo=UTC)
_CFG = {"comfort_heat": 68.0, "comfort_cool": 74.0, "sleep_heat": 64.0, "sleep_cool": 74.0}


def _minutes(indoor: float, mode: str, swings: dict | None = None) -> float:
    coord, fake_hass, scheduler, _events = build_headless_coordinator(
        config=_CFG,
        start_time=_DAY,
        skip_startup_coalesce=True,
        climate_state=mode,
        climate_attributes={"current_temperature": indoor},
    )
    daily = [
        {"datetime": (_DAY + timedelta(days=i)).isoformat(), "temperature": 60 + i, "templow": 45} for i in range(5)
    ]
    fake_hass.states.set(
        "weather.test_forecast", FakeState(state="sunny", attributes={"temperature": 55, "forecast": daily})
    )
    coord._get_hourly_forecast_data = AsyncMock(
        return_value=[{"datetime": (_DAY + timedelta(hours=k)).isoformat(), "temperature": 50.0} for k in range(-2, 30)]
    )
    coord._last_violation_check = None
    coord._thermostat_hvac_mode = lambda: mode  # CA re-commands the mode during a refresh; pin what the gate sees
    real_model = coord.learning.get_thermal_model
    if swings is not None:

        def _model(*a, **k):
            return {**real_model(*a, **k), **swings}

        coord.learning.get_thermal_model = _model
    before = coord._today_record.comfort_violations_minutes if coord._today_record else 0.0
    with scheduler.installed():
        run_coro(coord._async_update_data())
        scheduler.advance_by(0)
    return coord._today_record.comfort_violations_minutes - before


class TestComfortMinutesMirror:
    def test_a_room_inside_the_heat_swing_adds_no_violation_minutes(self) -> None:
        assert _minutes(67.0, "heat", {"swing_heat_f": 1.4}) == 0.0

    def test_a_room_inside_the_cool_swing_adds_no_violation_minutes(self) -> None:
        assert _minutes(75.0, "cool", {"swing_cool_f": 1.4}) == 0.0

    def test_a_tiny_learned_swing_is_floored_at_half_a_degree_for_minutes_too(self) -> None:
        assert _minutes(67.6, "heat", {"swing_heat_f": 0.3}) == 0.0  # 0.4 under, floor 0.5
        assert _minutes(67.4, "heat", {"swing_heat_f": 0.3}) == 30.0

    def test_beyond_the_swing_still_counts(self) -> None:
        assert _minutes(66.3, "heat", {"swing_heat_f": 1.4}) == 30.0

    def test_thermostat_off_still_counts_a_small_deviation(self) -> None:
        assert _minutes(67.0, "off", {"swing_heat_f": 1.4}) == 30.0

    def test_wrong_mode_still_counts(self) -> None:
        assert _minutes(67.0, "cool", {"swing_heat_f": 1.4}) == 30.0


# ---- the pure rule ----------------------------------------------------------------------------------------------


class TestPureRule:
    def test_swing_tolerance_resolution(self) -> None:
        assert swing_tolerance(1.4) == (1.4, SWING_SOURCE_LEARNED)
        assert swing_tolerance(None) == (1.5, SWING_SOURCE_DEFAULT)
        assert swing_tolerance(0.3) == (0.5, SWING_SOURCE_LEARNED)
        assert swing_tolerance(True) == (1.5, SWING_SOURCE_DEFAULT)
        assert swing_tolerance("x") == (1.5, SWING_SOURCE_DEFAULT)

    @pytest.mark.parametrize(
        ("direction", "mode", "expected"),
        [
            ("heat", "heat", True),
            ("heat", "HEAT_COOL", True),
            ("heat", "auto", True),
            ("heat", "cool", False),
            ("heat", "off", False),
            ("heat", None, False),
            ("cool", "cool", True),
            ("cool", "heat_cool", True),
            ("cool", "heat", False),
        ],
    )
    def test_mode_gate(self, direction: str, mode, expected: bool) -> None:
        assert thermostat_holds_direction(direction, mode) is expected

    def test_in_band_is_not_a_hysteresis_deviation(self) -> None:
        assert within_thermostat_hysteresis(70.0, 68.0, 74.0, 1.4, 1.4, "heat_cool") is False

    def test_one_sided_directions_use_their_own_swing(self) -> None:
        assert within_thermostat_hysteresis(67.0, 68.0, 74.0, 1.4, 0.2, "heat_cool") is True
        assert within_thermostat_hysteresis(75.0, 68.0, 74.0, 1.4, 0.2, "heat_cool") is False

    def test_missing_band_edge_is_false(self) -> None:
        assert within_thermostat_hysteresis(60.0, None, 74.0, 1.4, 1.4, "heat") is False


# ---- the 2.5 F cap (owner decision 2026-11-02) -------------------------------------------------------------------
# Occupant risk guarded: a mis-learned large swing (e.g. 4.0 F) must not silence the incident, the violation minutes
# and the stall alert while the house sits 3.5 F under the floor with the heat idle.


class TestToleranceCap:
    def test_pure_rule_caps_only_above_two_and_a_half(self) -> None:
        assert swing_tolerance(2.4) == (2.4, SWING_SOURCE_LEARNED)
        assert swing_tolerance(2.5) == (2.5, SWING_SOURCE_LEARNED)
        assert swing_tolerance(2.6) == (2.5, "learned (capped)")
        assert swing_tolerance(4.0) == (2.5, "learned (capped)")
        assert within_thermostat_hysteresis(65.5, 68.0, 74.0, 4.0, None, "heat") is True  # exactly the cap
        assert within_thermostat_hysteresis(65.4, 68.0, 74.0, 4.0, None, "heat") is False
        assert within_thermostat_hysteresis(77.0, 68.0, 74.0, None, 4.0, "cool") is False

    def test_four_degree_swing_three_and_a_half_under_is_an_incident(self) -> None:
        inc = _scan(_coord(mode="heat", swing_heat=4.0), 64.5)
        assert [e["incident_class"] for e in inc] == ["comfort_undertemp"]
        assert inc[0]["swing_tolerance_f"] == 2.5
        assert inc[0]["swing_tolerance_source"] == "learned (capped)"
        assert inc[0]["swing_tolerance_applied"] is True

    def test_a_2_4_swing_is_used_as_is(self) -> None:
        assert _scan(_coord(mode="heat", swing_heat=2.4), 65.7) == []  # 2.3 under < 2.4
        inc = _scan(_coord(mode="heat", swing_heat=2.4), 65.5)  # 2.5 under > 2.4
        assert inc[0]["swing_tolerance_f"] == 2.4 and inc[0]["swing_tolerance_source"] == SWING_SOURCE_LEARNED

    def test_the_2_5_boundary(self) -> None:
        assert _scan(_coord(mode="heat", swing_heat=4.0), 65.5) == []  # exactly 2.5 under: inclusive
        assert [e["incident_class"] for e in _scan(_coord(mode="heat", swing_heat=4.0), 65.4)] == ["comfort_undertemp"]
        assert _scan(_coord(mode="cool", swing_cool=4.0), 76.5) == []
        assert [e["incident_class"] for e in _scan(_coord(mode="cool", swing_cool=4.0), 76.6)] == ["comfort_violation"]

    def test_four_degree_swing_minutes_still_count(self) -> None:
        assert _minutes(64.5, "heat", {"swing_heat_f": 4.0}) == 30.0
