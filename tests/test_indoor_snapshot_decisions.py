"""Issue #1036: decisions carry the indoor/outdoor reading they were made from.

Occupant experience: a thermostat/sensor that drops out right after a "welcome home"
restore used to crash the notification step with a TypeError (the occupancy-toggle
listener aborted and a state save was skipped). The nat-vent ceiling escalation had the
same latent shape. The shells now log/emit the value the *decision* used instead of
re-reading the sensor after an await.

All tests drive the REAL pure functions and the REAL shells.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import custom_components.climate_advisor.automation as _automation_mod
from custom_components.climate_advisor.automation import AutomationEngine
from custom_components.climate_advisor.classification_fsm import (
    CeilingGuardEligibility,
    ClassificationDecision,
    ClassificationFsmEventKind,
)
from custom_components.climate_advisor.classifier import DayClassification
from custom_components.climate_advisor.occupancy_fsm import (
    HomeInputs,
    HomeNotifyOutcome,
    decide_home_dispatch,
)
from custom_components.climate_advisor.ode_ceiling_guard import (
    OdeCeilingGuardInputs,
    OdeCeilingGuardOutcome,
    decide_ode_ceiling_guard,
)

_NOW = datetime(2026, 7, 15, 14, 0, 0, tzinfo=UTC)


# ── Pure decision functions ──────────────────────────────────────────────────


def _home_inputs(**overrides) -> HomeInputs:
    base = {
        "has_classification": True,
        "hvac_mode": "heat",
        "indoor_temp_f": 69.0,
        "comfort_f": 70.0,
        "setback_f": 60.0,
        "debounce_seconds": 3600.0,
        "seconds_since_last_notified": None,
    }
    base.update(overrides)
    return HomeInputs(**base)


class TestHomeDecisionCarriesIndoor:
    def test_near_comfort_carries_indoor(self):
        d = decide_home_dispatch(_home_inputs(indoor_temp_f=69.0))
        assert d.notify is HomeNotifyOutcome.SUPPRESSED_NEAR_COMFORT
        assert d.indoor_temp_f == 69.0

    def test_debounce_carries_indoor(self):
        d = decide_home_dispatch(_home_inputs(indoor_temp_f=62.0, seconds_since_last_notified=10.0))
        assert d.notify is HomeNotifyOutcome.SUPPRESSED_DEBOUNCE
        assert d.indoor_temp_f == 62.0

    def test_send_carries_indoor(self):
        d = decide_home_dispatch(_home_inputs(indoor_temp_f=62.0))
        assert d.notify is HomeNotifyOutcome.SEND
        assert d.indoor_temp_f == 62.0

    def test_send_with_unreadable_indoor_is_none(self):
        d = decide_home_dispatch(_home_inputs(indoor_temp_f=None))
        assert d.notify is HomeNotifyOutcome.SEND
        assert d.indoor_temp_f is None

    def test_no_classification_defaults_none(self):
        d = decide_home_dispatch(_home_inputs(has_classification=False))
        assert d.notify is HomeNotifyOutcome.NONE
        assert d.indoor_temp_f is None


def _ceiling_inputs(**overrides) -> OdeCeilingGuardInputs:
    # Breach 20 min out: inside the 30 min minimum lead window -> ESCALATE.
    predicted = [
        {"ts": (_NOW + timedelta(minutes=20)).isoformat(), "temp": 79.0},
    ]
    base = {
        "predicted_indoor": predicted,
        "hvac_mode": "off",
        "k_passive": -0.1,
        "confidence_k_passive": "high",
        "k_passive_via_bridge": False,
        "k_active_cool": 4.0,
        "comfort_cool": 74.0,
        "outdoor": 90.0,
        "indoor": 76.0,
        "natural_vent_active": False,
        "ceiling_threshold": 74.0,
        "now": _NOW,
    }
    base.update(overrides)
    return OdeCeilingGuardInputs(**base)


class TestCeilingDecisionCarriesReadings:
    def test_escalate_carries_indoor_outdoor(self):
        d = decide_ode_ceiling_guard(_ceiling_inputs())
        assert d.outcome is OdeCeilingGuardOutcome.ESCALATE
        assert (d.indoor, d.outdoor) == (76.0, 90.0)

    def test_dormant_carries_indoor_outdoor(self):
        d = decide_ode_ceiling_guard(
            _ceiling_inputs(outdoor=60.0, indoor=72.0, natural_vent_active=True, ceiling_threshold=74.0)
        )
        assert d.outcome is OdeCeilingGuardOutcome.DORMANT
        assert (d.indoor, d.outdoor) == (72.0, 60.0)

    def test_standing_by_carries_indoor_outdoor(self):
        far = [{"ts": (_NOW + timedelta(hours=20)).isoformat(), "temp": 79.0}]
        d = decide_ode_ceiling_guard(_ceiling_inputs(predicted_indoor=far))
        assert d.outcome is OdeCeilingGuardOutcome.STANDING_BY
        assert (d.indoor, d.outdoor) == (76.0, 90.0)

    def test_no_breach_carries_indoor_outdoor(self):
        flat = [{"ts": (_NOW + timedelta(hours=1)).isoformat(), "temp": 70.0}]
        d = decide_ode_ceiling_guard(_ceiling_inputs(predicted_indoor=flat))
        assert d.outcome is OdeCeilingGuardOutcome.NO_BREACH_PREDICTED
        assert (d.indoor, d.outdoor) == (76.0, 90.0)

    def test_no_ceiling_threshold_carries_indoor_outdoor(self):
        d = decide_ode_ceiling_guard(_ceiling_inputs(ceiling_threshold=None))
        assert d.outcome is OdeCeilingGuardOutcome.NO_CEILING_THRESHOLD
        assert (d.indoor, d.outdoor) == (76.0, 90.0)

    def test_missing_temps_indoor_none_carries_outdoor(self):
        d = decide_ode_ceiling_guard(_ceiling_inputs(indoor=None))
        assert d.outcome is OdeCeilingGuardOutcome.MISSING_TEMPS
        assert (d.indoor, d.outdoor) == (None, 90.0)

    def test_missing_temps_outdoor_none_carries_indoor(self):
        d = decide_ode_ceiling_guard(_ceiling_inputs(outdoor=None))
        assert d.outcome is OdeCeilingGuardOutcome.MISSING_TEMPS
        assert (d.indoor, d.outdoor) == (76.0, None)

    def test_model_ineligible_carries_readings(self):
        d = decide_ode_ceiling_guard(_ceiling_inputs(k_passive=None))
        assert d.outcome is OdeCeilingGuardOutcome.MODEL_INELIGIBLE
        assert (d.indoor, d.outdoor) == (76.0, 90.0)

    def test_not_applicable_leaves_none(self):
        d = decide_ode_ceiling_guard(_ceiling_inputs(hvac_mode="heat"))
        assert d.outcome is OdeCeilingGuardOutcome.NOT_APPLICABLE
        assert (d.indoor, d.outdoor) == (None, None)


# ── Shells ───────────────────────────────────────────────────────────────────


def _make_engine(config_overrides: dict | None = None) -> AutomationEngine:
    hass = MagicMock()
    hass.services = MagicMock()
    hass.services.async_call = AsyncMock()
    hass.async_create_task = MagicMock(side_effect=lambda coro: coro.close())
    climate_state = MagicMock()
    climate_state.state = "off"
    climate_state.attributes = {"current_temperature": 76.0, "temperature": None}
    hass.states.get = MagicMock(return_value=climate_state)
    config = {
        "comfort_heat": 70,
        "comfort_cool": 74,
        "setback_heat": 60,
        "setback_cool": 80,
        "notify_service": "notify.notify",
        "indoor_temp_source": "climate_fallback",
        "temp_unit": "fahrenheit",
    }
    config.update(config_overrides or {})
    return AutomationEngine(
        hass=hass,
        climate_entity="climate.thermostat",
        weather_entity="weather.forecast_home",
        door_window_sensors=[],
        notify_service="notify.notify",
        config=config,
    )


def _heat_classification() -> DayClassification:
    obj = object.__new__(DayClassification)
    obj.day_type = "cold"
    obj.hvac_mode = "heat"
    return obj


class TestWelcomeHomeNearComfortUsesDecisionValue:
    def test_indoor_vanishing_after_restore_does_not_crash(self, caplog):
        """Occupant: thermostat drops out right after the restore; the welcome-home
        step must still finish (timestamp set, no TypeError)."""
        engine = _make_engine()
        engine._current_classification = _heat_classification()

        # Real decision from a healthy read.
        decision = decide_home_dispatch(_home_inputs(indoor_temp_f=69.0, comfort_f=70.0, setback_f=60.0))
        assert decision.notify is HomeNotifyOutcome.SUPPRESSED_NEAR_COMFORT

        # The restore await is where the sensor "drops out".
        async def _restore_then_blank(*_a, **_k):
            engine._get_indoor_temp_f = MagicMock(return_value=None)

        engine._set_temperature_for_mode = AsyncMock(side_effect=_restore_then_blank)
        engine._notify = AsyncMock()

        with (
            patch.object(_automation_mod.dt_util, "now", return_value=_NOW),
            caplog.at_level(logging.INFO, logger=_automation_mod.__name__),
        ):
            asyncio.run(engine._apply_occupancy_home_decision(decision))

        assert engine._last_welcome_home_notified == _NOW
        engine._notify.assert_not_called()
        assert "indoor 69.0" in caplog.text
        assert "dist_comfort=1.0" in caplog.text

    def test_comfort_restored_event_uses_decision_value(self):
        engine = _make_engine()
        engine._current_classification = _heat_classification()
        decision = decide_home_dispatch(_home_inputs(indoor_temp_f=69.0))

        async def _restore_then_blank(*_a, **_k):
            engine._get_indoor_temp_f = MagicMock(return_value=None)

        engine._set_temperature_for_mode = AsyncMock(side_effect=_restore_then_blank)
        events: list[tuple] = []
        engine._emit_event_callback = lambda name, payload: events.append((name, payload))

        with patch.object(_automation_mod.dt_util, "now", return_value=_NOW):
            asyncio.run(engine._apply_occupancy_home_decision(decision))

        restored = [p for n, p in events if n == "occupancy_comfort_restored"]
        assert len(restored) == 1
        assert restored[0]["indoor_f"] == 69.0


class TestHandleOccupancyHomeFullChainCelsius:
    """Occupant (Celsius home): thermostat reads 21.5C (70.7F) on return. The sensor
    drops out during the comfort-restore write. The REAL handle_occupancy_home() chain
    (resolve -> decide -> apply) must still finish and report the decision's 70.7F."""

    def test_sensor_blanked_during_restore_still_completes(self, caplog):
        engine = _make_engine({"temp_unit": "celsius"})
        climate_state = engine.hass.states.get.return_value
        climate_state.attributes = {"current_temperature": 21.5, "temperature": None}
        engine._current_classification = _heat_classification()
        engine._notify = AsyncMock()
        events: list[tuple] = []
        engine._emit_event_callback = lambda name, payload: events.append((name, payload))

        async def _restore_then_blank(*_a, **_k):
            # The thermostat drops out right after the restore write: any re-read is None.
            climate_state.attributes = {"current_temperature": None, "temperature": None}
            climate_state.state = "unavailable"

        engine._set_temperature_for_mode = _restore_then_blank

        with (
            patch.object(_automation_mod.dt_util, "now", return_value=_NOW),
            caplog.at_level(logging.INFO, logger=_automation_mod.__name__),
        ):
            asyncio.run(engine.handle_occupancy_home())

        assert engine._last_welcome_home_notified == _NOW
        engine._notify.assert_not_called()
        restored = [p for n, p in events if n == "occupancy_comfort_restored"]
        assert len(restored) == 1
        assert restored[0]["indoor_f"] == pytest.approx(70.7, abs=0.05)
        assert "indoor 70.7" in caplog.text


class TestCeilingEscalationUsesDecisionSnapshot:
    def test_escalation_with_indoor_vanishing_uses_snapshot(self):
        engine = _make_engine()
        engine._natural_vent_active = True
        engine._thermal_model = {"k_passive": -0.1, "confidence_k_passive": "high", "k_active_cool": 4.0}
        engine._last_outdoor_temp = 12.3  # must NOT leak into the payload
        engine._get_indoor_temp_f = MagicMock(return_value=None)
        engine._deactivate_fan = AsyncMock(return_value=_automation_mod.FanCommandResult.EXECUTED)
        engine._set_hvac_mode = AsyncMock()
        engine._set_temperature = AsyncMock()
        events: list[tuple] = []
        engine._emit_event_callback = lambda name, payload: events.append((name, payload))

        ceiling = decide_ode_ceiling_guard(_ceiling_inputs(natural_vent_active=True))
        assert ceiling.outcome is OdeCeilingGuardOutcome.ESCALATE
        decision = ClassificationDecision(
            event_kind=ClassificationFsmEventKind.CYCLE_EVALUATED,
            gate=MagicMock(),
            ceiling_eligibility=CeilingGuardEligibility.EVALUATED,
            ceiling_decision=ceiling,
            at=_NOW,
        )

        with patch.object(_automation_mod.dt_util, "now", return_value=_NOW):
            asyncio.run(
                engine._apply_ode_ceiling_guard_decision(
                    MagicMock(hvac_mode="off"), predicted_indoor=[{"ts": "x"}], decision=decision
                )
            )

        engine._get_indoor_temp_f.assert_not_called()
        reason = engine._deactivate_fan.call_args.kwargs["reason"]
        assert "76.0" in reason
        payload = [p for n, p in events if n == "nat_vent_ceiling_escalation"]
        assert len(payload) == 1
        assert payload[0]["indoor"] == 76.0
        assert payload[0]["outdoor"] == 90.0


@pytest.mark.parametrize("attr", ["_indoor_f_for_event"])
def test_legacy_event_indoor_helper_is_gone(attr):
    """Issue #1038: the unit-blind helper must not come back."""
    assert not hasattr(AutomationEngine, attr)
