"""Issue #1093: with Prefer savings on, the ODE ceiling guard defends the savings-adjusted awake ceiling.

Comfort 70-75 with Prefer savings on arms the thermostat to 77 (``awake_comfort_edges()``, Issue #1087).
Before #1093 the guard's dormancy test used 77 but its breach scan, lead time and cool setpoint still
used 75, so on a WARM ``off`` day it pre-cooled for a breach the thermostat itself would not act on and
wrote a 75 setpoint underneath a 77 band. These tests pin every use of the ceiling to the one number,
with literal expected values (never recomputing the margin).

Followers: the warm-day briefing sentences live in ``tests/test_briefing.py``
(``TestWarmDayAcHandoffQuotesSavingsCeiling``); the plan/card time is pinned here.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

import custom_components.climate_advisor.automation as _automation_mod
from custom_components.climate_advisor.automation import AutomationEngine
from custom_components.climate_advisor.classifier import DayClassification
from custom_components.climate_advisor.const import FAN_MODE_BOTH, FAN_MODE_HVAC, FAN_MODE_WHOLE_HOUSE
from custom_components.climate_advisor.nat_vent_plan import compute_nat_vent_plan
from custom_components.climate_advisor.ode_ceiling_guard import OdeCeilingGuardOutcome
from tests.test_forecast_chart_integration import (
    _classification as _fc_classification,
)
from tests.test_forecast_chart_integration import (
    _get_coordinator_module,
    _make_full_chart_coord,
    _patched_dt_util,
)

_NOW = datetime(2026, 7, 15, 12, 0, 0, tzinfo=UTC)


def _classification() -> DayClassification:
    obj = object.__new__(DayClassification)
    obj.day_type = "warm"
    obj.hvac_mode = "off"
    return obj


def _curve(*offsets_and_temps: tuple[int, float]) -> list[dict]:
    """Predicted-indoor points at +N minutes from _NOW."""
    return [{"ts": (_NOW + timedelta(minutes=m)).isoformat(), "temp": t} for m, t in offsets_and_temps]


def _make_engine(
    *, savings: bool, fan_mode: str = FAN_MODE_HVAC, sensors: list[str] | None = None, sensor_open: bool = False
) -> AutomationEngine:
    """Real AutomationEngine, HA layer mocked: comfort 70-75, trained model, nat-vent not running."""
    hass = MagicMock()
    hass.services = MagicMock()
    hass.services.async_call = AsyncMock()
    hass.async_create_task = MagicMock(side_effect=lambda coro: coro.close())
    state = MagicMock()
    state.state = "off"
    state.attributes = {"current_temperature": 74.0, "temperature": None}
    hass.states = MagicMock()
    hass.states.get = MagicMock(return_value=state)
    config = {
        "comfort_heat": 70.0,
        "comfort_cool": 75.0,
        "setback_heat": 60,
        "setback_cool": 80,
        "natural_vent_delta": 3.0,
        "notify_service": "notify.notify",
        "temp_unit": "fahrenheit",
        "aggressive_savings": savings,
        "fan_mode": fan_mode,
        "door_window_sensors": list(sensors or []),
    }
    engine = AutomationEngine(
        hass=hass,
        climate_entity="climate.thermostat",
        weather_entity="weather.forecast_home",
        door_window_sensors=[],
        notify_service="notify.notify",
        config=config,
    )
    engine._sensor_check_callback = lambda: sensor_open
    # Sealed = sensors configured and none open (the coordinator's real predicate is pinned separately
    # in TestSealedNeedsKnownClosedSensors, which wires the real callback).
    engine._sensors_known_closed_callback = lambda: bool(sensors) and not sensor_open
    engine._thermal_model = {"k_passive": -0.5, "confidence_k_passive": "high", "k_active_cool": -2.0}
    engine._natural_vent_active = False
    engine._last_outdoor_temp = 90.0
    engine._get_indoor_temp_f = lambda: 74.0
    engine._set_temperature = AsyncMock()
    engine._set_hvac_mode = AsyncMock()
    return engine


async def _run(engine: AutomationEngine, predicted: list[dict]):
    events: list[tuple] = []
    engine._emit_event_callback = lambda name, payload: events.append((name, payload))
    classification = _classification()
    decision = engine._resolve_classification_fsm_state(classification=classification, predicted_indoor=predicted)
    await engine._apply_ode_ceiling_guard_decision(classification, predicted, decision)
    return decision.ceiling_decision, events


@pytest.fixture(autouse=True)
def _freeze_now(monkeypatch):
    monkeypatch.setattr(_automation_mod.dt_util, "now", lambda: _NOW)


# Peaks at 76 at +30 min (a breach of 75, NOT of 77), reaches 78 at +90 min (a breach of 77).
_BREACH_77 = _curve((30, 76.0), (90, 78.0))
# Never exceeds 76: a breach of 75 only.
_BREACH_75_ONLY = _curve((30, 76.0), (90, 76.0))


class TestSavingsOnGuardDefendsTheSavingsCeiling:
    def test_escalates_for_a_77_breach_and_writes_77(self):
        engine = _make_engine(savings=True)
        decision, events = asyncio.run(_run(engine, _BREACH_77))
        assert decision.outcome is OdeCeilingGuardOutcome.ESCALATE
        assert decision.ceiling == 77.0
        engine._set_temperature.assert_awaited_once()
        assert engine._set_temperature.await_args.args[0] == 77.0
        fired = [p for n, p in events if n == "ceiling_guard_fired"]
        assert len(fired) == 1
        assert fired[0]["new_setpoint_f"] == 77.0

    def test_a_75_only_breach_does_not_escalate(self):
        engine = _make_engine(savings=True)
        decision, _events = asyncio.run(_run(engine, _BREACH_75_ONLY))
        assert decision.outcome is OdeCeilingGuardOutcome.NO_BREACH_PREDICTED
        engine._set_temperature.assert_not_awaited()

    def test_lead_time_is_measured_from_the_savings_ceiling(self):
        # indoor 74, ceiling 77, k_active_cool -2.0 F/h: (3 / 2) * 60 * 1.3 = 117 min.
        engine = _make_engine(savings=True)
        decision, _events = asyncio.run(_run(engine, _BREACH_77))
        assert decision.lead_min == pytest.approx(117.0)


class TestSavingsOffControlStaysAt75:
    def test_escalates_for_a_75_breach_and_writes_75(self):
        engine = _make_engine(savings=False)
        decision, events = asyncio.run(_run(engine, _BREACH_75_ONLY))
        assert decision.outcome is OdeCeilingGuardOutcome.ESCALATE
        assert decision.ceiling == 75.0
        assert engine._set_temperature.await_args.args[0] == 75.0
        fired = [p for n, p in events if n == "ceiling_guard_fired"]
        assert fired[0]["new_setpoint_f"] == 75.0

    def test_lead_time_is_measured_from_the_plain_ceiling(self):
        # indoor 74, ceiling 75: (1 / 2) * 60 * 1.3 = 39 min (above the 30 min floor).
        engine = _make_engine(savings=False)
        decision, _events = asyncio.run(_run(engine, _BREACH_75_ONLY))
        assert decision.lead_min == pytest.approx(39.0)


class TestWholeHouseFanHomeFollowsTheSealedState:
    """Issue #1093: a WHF/BOTH home is a non-WHF home while every monitored sensor is closed (the fan
    never runs on a sealed house); any open sensor, or no sensors configured, keeps today's no-handoff."""

    _SENSORS = ["binary_sensor.front_door", "binary_sensor.office_window"]

    @pytest.mark.parametrize("fan_mode", [FAN_MODE_WHOLE_HOUSE, FAN_MODE_BOTH])
    def test_sealed_savings_on_escalates_at_77(self, fan_mode):
        engine = _make_engine(savings=True, fan_mode=fan_mode, sensors=self._SENSORS)
        decision, _events = asyncio.run(_run(engine, _BREACH_77))
        assert decision.outcome is OdeCeilingGuardOutcome.ESCALATE
        assert engine._set_temperature.await_args.args[0] == 77.0

    def test_sealed_savings_off_escalates_at_75(self):
        engine = _make_engine(savings=False, fan_mode=FAN_MODE_WHOLE_HOUSE, sensors=self._SENSORS)
        decision, _events = asyncio.run(_run(engine, _BREACH_75_ONLY))
        assert decision.outcome is OdeCeilingGuardOutcome.ESCALATE
        assert engine._set_temperature.await_args.args[0] == 75.0

    @pytest.mark.parametrize("savings", [True, False])
    def test_any_sensor_open_keeps_no_handoff(self, savings):
        engine = _make_engine(savings=savings, fan_mode=FAN_MODE_WHOLE_HOUSE, sensors=self._SENSORS, sensor_open=True)
        decision, _events = asyncio.run(_run(engine, _BREACH_77))
        assert decision.outcome is OdeCeilingGuardOutcome.NO_CEILING_THRESHOLD
        engine._set_temperature.assert_not_awaited()

    @pytest.mark.parametrize("savings", [True, False])
    def test_no_sensors_configured_is_unknown_not_sealed(self, savings):
        engine = _make_engine(savings=savings, fan_mode=FAN_MODE_WHOLE_HOUSE, sensors=[])
        decision, _events = asyncio.run(_run(engine, _BREACH_77))
        assert decision.outcome is OdeCeilingGuardOutcome.NO_CEILING_THRESHOLD
        engine._set_temperature.assert_not_awaited()

    def test_whf_session_owning_the_thermostat_never_reaches_the_guard(self):
        # The classification FSM defers before the ceiling guard whenever a WHF session owns the
        # thermostat, so the AC cannot be started underneath a running fan.
        engine = _make_engine(savings=True, fan_mode=FAN_MODE_WHOLE_HOUSE, sensors=self._SENSORS)
        engine._whf_owns_hvac = lambda: True
        decision, events = asyncio.run(_run(engine, _BREACH_77))
        assert decision is None
        engine._set_temperature.assert_not_awaited()
        engine._set_hvac_mode.assert_not_awaited()
        assert not [n for n, _p in events if n == "ceiling_guard_fired"]

    def test_window_opening_after_the_guard_fired_still_pauses_hvac(self):
        engine = _make_engine(savings=True, fan_mode=FAN_MODE_WHOLE_HOUSE, sensors=self._SENSORS)
        decision, _events = asyncio.run(_run(engine, _BREACH_77))
        assert decision.outcome is OdeCeilingGuardOutcome.ESCALATE
        engine._set_hvac_mode.reset_mock()
        engine._sensor_check_callback = lambda: True
        engine.hass.states.get.return_value.state = "cool"
        asyncio.run(engine.handle_door_window_open("binary_sensor.front_door"))
        assert engine._paused_by_door is True
        engine._set_hvac_mode.assert_awaited_with("off", reason=engine._set_hvac_mode.await_args.kwargs["reason"])

    def test_non_whf_home_is_unaffected_by_open_sensors(self):
        engine = _make_engine(savings=True, sensors=self._SENSORS, sensor_open=True)
        decision, _events = asyncio.run(_run(engine, _BREACH_77))
        assert decision.outcome is OdeCeilingGuardOutcome.ESCALATE


def _wired_whf_engine(states: dict[str, str | None], *, inverted: bool = False):
    """A WHF engine whose sealed-house callback is the coordinator's REAL ``_all_sensors_known_closed``
    reading a fake HA state table (``None`` = entity missing from HA)."""
    coordinator_cls = _get_coordinator_module().ClimateAdvisorCoordinator
    coord = object.__new__(coordinator_cls)
    coord.config = {"sensor_polarity_inverted": inverted}
    coord._resolved_sensors = list(states)

    def _get(entity_id):
        raw = states[entity_id]
        if raw is None:
            return None
        st = MagicMock()
        st.state = raw
        return st

    coord.hass = MagicMock()
    coord.hass.states.get = _get
    engine = _make_engine(savings=True, fan_mode=FAN_MODE_WHOLE_HOUSE, sensors=list(states))
    engine._sensors_known_closed_callback = coord._all_sensors_known_closed
    engine._sensor_check_callback = coord._any_sensor_open
    return engine


class TestSealedNeedsKnownClosedSensors:
    """A sensor the integration cannot read (Zigbee hub down: unavailable/unknown/missing) is NOT closed,
    so a WHF home with it stays on the no-handoff path instead of starting the AC with windows maybe open."""

    _TWO = ["binary_sensor.front_door", "binary_sensor.office_window"]

    def test_all_closed_escalates(self):
        engine = _wired_whf_engine({self._TWO[0]: "off", self._TWO[1]: "off"})
        decision, _events = asyncio.run(_run(engine, _BREACH_77))
        assert decision.outcome is OdeCeilingGuardOutcome.ESCALATE
        assert engine._set_temperature.await_args.args[0] == 77.0

    def test_one_sensor_unavailable_is_not_sealed(self):
        engine = _wired_whf_engine({self._TWO[0]: "off", self._TWO[1]: "unavailable"})
        decision, _events = asyncio.run(_run(engine, _BREACH_77))
        assert decision.outcome is OdeCeilingGuardOutcome.NO_CEILING_THRESHOLD
        engine._set_temperature.assert_not_awaited()

    def test_all_sensors_unknown_is_not_sealed(self):
        engine = _wired_whf_engine({self._TWO[0]: "unknown", self._TWO[1]: "unknown"})
        decision, _events = asyncio.run(_run(engine, _BREACH_77))
        assert decision.outcome is OdeCeilingGuardOutcome.NO_CEILING_THRESHOLD
        engine._set_temperature.assert_not_awaited()

    def test_a_sensor_missing_from_ha_is_not_sealed(self):
        engine = _wired_whf_engine({self._TWO[0]: "off", self._TWO[1]: None})
        decision, _events = asyncio.run(_run(engine, _BREACH_77))
        assert decision.outcome is OdeCeilingGuardOutcome.NO_CEILING_THRESHOLD

    def test_an_open_sensor_is_not_sealed(self):
        engine = _wired_whf_engine({self._TWO[0]: "off", self._TWO[1]: "on"})
        decision, _events = asyncio.run(_run(engine, _BREACH_77))
        assert decision.outcome is OdeCeilingGuardOutcome.NO_CEILING_THRESHOLD

    def test_inverted_polarity_reads_off_as_open(self):
        engine = _wired_whf_engine({self._TWO[0]: "on", self._TWO[1]: "on"}, inverted=True)
        decision, _events = asyncio.run(_run(engine, _BREACH_77))
        assert decision.outcome is OdeCeilingGuardOutcome.ESCALATE
        engine = _wired_whf_engine({self._TWO[0]: "on", self._TWO[1]: "off"}, inverted=True)
        decision, _events = asyncio.run(_run(engine, _BREACH_77))
        assert decision.outcome is OdeCeilingGuardOutcome.NO_CEILING_THRESHOLD

    def test_unavailable_sensor_does_not_read_as_open_for_the_whf_stop(self):
        # The WHF safety-stop direction is unchanged: an unreadable sensor is not "open".
        engine = _wired_whf_engine({self._TWO[0]: "off", self._TWO[1]: "unavailable"})
        assert engine._any_monitored_sensor_open() is False

    def test_without_the_callback_wired_the_house_is_not_sealed(self):
        engine = _make_engine(savings=True, fan_mode=FAN_MODE_WHOLE_HOUSE, sensors=self._TWO)
        engine._sensors_known_closed_callback = None
        assert engine._whf_house_sealed_with_known_sensors() is False


class TestCeilingThresholdHasOneSource:
    @pytest.mark.parametrize(("savings", "expected"), [(True, 77.0), (False, 75.0)])
    def test_matches_the_armed_awake_ceiling(self, savings, expected):
        engine = _make_engine(savings=savings)
        assert engine._ceiling_threshold(75.0) == expected

    def test_default_keeps_the_whf_archetype_none_unless_house_sealed_is_passed(self):
        # Only the callers that know the sealed state (the guard, the chart forecast) pass house_sealed.
        engine = _make_engine(savings=True, fan_mode=FAN_MODE_WHOLE_HOUSE, sensors=["binary_sensor.door"])
        assert engine._ceiling_threshold(75.0) is None
        assert engine._ceiling_threshold(75.0, house_sealed=True) == 77.0


class TestGuardLoggingAndEventPayload:
    def test_no_ceiling_line_says_why_unknown_sensor(self, caplog):
        engine = _wired_whf_engine({"binary_sensor.a": "off", "binary_sensor.b": "unavailable"})
        with caplog.at_level("DEBUG"):
            asyncio.run(_run(engine, _BREACH_77))
        assert any("a door/window sensor state is unknown or unavailable" in r.getMessage() for r in caplog.records)

    def test_no_ceiling_line_says_why_window_open(self, caplog):
        engine = _wired_whf_engine({"binary_sensor.a": "off", "binary_sensor.b": "on"})
        with caplog.at_level("DEBUG"):
            asyncio.run(_run(engine, _BREACH_77))
        assert any("a door/window is open" in r.getMessage() for r in caplog.records)

    def test_no_ceiling_line_says_why_no_sensors(self, caplog):
        engine = _make_engine(savings=True, fan_mode=FAN_MODE_WHOLE_HOUSE, sensors=[])
        with caplog.at_level("DEBUG"):
            asyncio.run(_run(engine, _BREACH_77))
        assert any("no door/window sensors configured" in r.getMessage() for r in caplog.records)

    def test_eval_line_carries_house_sealed(self, caplog):
        engine = _wired_whf_engine({"binary_sensor.a": "off", "binary_sensor.b": "off"})
        with caplog.at_level("DEBUG"):
            asyncio.run(_run(engine, _BREACH_77))
        assert any("ceiling=77.0, house_sealed=True" in r.getMessage() for r in caplog.records)

    def test_escalation_payload_carries_ceiling_and_the_legacy_key(self):
        # Savings off, HVAC-fan home, nat-vent running but indoor already past the 75 ceiling: the guard
        # ends the session and emits the escalation event.
        engine = _make_engine(savings=False)
        engine._natural_vent_active = True
        engine._last_outdoor_temp = 70.0
        engine._deactivate_fan = AsyncMock(return_value=None)
        engine._get_indoor_temp_f = lambda: 76.0
        _decision, events = asyncio.run(_run(engine, _BREACH_75_ONLY))
        payload = [p for n, p in events if n == "nat_vent_ceiling_escalation"]
        assert len(payload) == 1
        assert payload[0]["ceiling"] == 75.0
        assert payload[0]["comfort_cool"] == 75.0


class TestChartForecastReadsTheSameSealedDecision:
    """The chart forecast must not disagree with the guard: get_chart_data() asks the engine's sealed
    predicate and passes it to the SAME _ceiling_threshold() the guard uses."""

    @pytest.mark.parametrize("sealed", [True, False])
    def test_forecast_walk_ceiling_is_resolved_with_the_engine_sealed_decision(self, sealed):
        coord_mod = _get_coordinator_module()
        coord = _make_full_chart_coord(daily_forecast_full=None)
        ae = coord.automation_engine
        ae._whf_house_sealed_with_known_sensors = MagicMock(return_value=sealed)
        now_p, as_local_p = _patched_dt_util(coord_mod)
        with now_p, as_local_p:
            coord.get_chart_data("24h")
        assert ae._ceiling_threshold.call_args.kwargs == {"house_sealed": sealed}


class TestPlanBreachTimeFollowsTheGuardCeiling:
    """The Next Automation card's "AC turns on to hold the ceiling" time is the plan's ceiling_breach_time."""

    _INDOOR = _curve((0, 72.0), (60, 74.0), (120, 76.0), (180, 76.0), (240, 78.0), (300, 79.0))
    _OUTDOOR = _curve((0, 60.0), (60, 62.0), (120, 64.0), (180, 66.0), (240, 68.0), (300, 70.0))

    def test_default_ceiling_is_comfort_cool(self):
        plan = compute_nat_vent_plan(self._INDOOR, self._OUTDOOR, 75.0)
        assert plan["ceiling_breach_time"] == _NOW + timedelta(minutes=120)

    def test_savings_ceiling_moves_the_breach_to_the_77_crossing(self):
        plan = compute_nat_vent_plan(self._INDOOR, self._OUTDOOR, 75.0, ceiling=77.0)
        assert plan["ceiling_breach_time"] == _NOW + timedelta(minutes=240)

    def test_precool_start_uses_the_same_ceiling(self):
        # k_active_cool -2.0, indoor now 72, ceiling 77: (5 / 2) * 60 * 1.3 = 195 min before the 240 min breach.
        plan = compute_nat_vent_plan(self._INDOOR, self._OUTDOOR, 75.0, k_active_cool=-2.0, ceiling=77.0)
        assert plan["precool_start_time"] == _NOW + timedelta(minutes=45)


class TestCoordinatorPlanCacheReadsTheArmedCeiling:
    """The cached plan the briefing and the Next Automation card both read is built with the armed ceiling."""

    @staticmethod
    def _plan(savings: bool):
        base = datetime(2026, 6, 1, 0, 0, 0)
        temps = [72.0] * 8 + [76.0] * 4 + [78.0] * 12
        coord = _make_full_chart_coord(daily_forecast_full=None)
        coord.config = {**coord.config, "comfort_heat": 70, "comfort_cool": 75, "aggressive_savings": savings}
        coord._current_classification = _fc_classification(hvac_mode="off")
        coord._last_predicted_indoor = [
            {"ts": (base + timedelta(hours=i)).isoformat(), "temp": t} for i, t in enumerate(temps)
        ]
        now_p, as_local_p = _patched_dt_util(_get_coordinator_module())
        with now_p, as_local_p:
            coord._compute_and_cache_nat_vent_plan()
        return coord._nat_vent_plan

    def test_savings_on_breach_is_the_77_crossing(self):
        assert self._plan(True)["ceiling_breach_time"] == datetime(2026, 6, 1, 12, 0, 0)

    def test_savings_off_breach_is_the_75_crossing(self):
        assert self._plan(False)["ceiling_breach_time"] == datetime(2026, 6, 1, 8, 0, 0)
