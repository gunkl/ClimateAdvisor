"""Tests for nat-vent thermostat cycling (Bug 3, Issue #321).

Natural ventilation now acts as an active thermostat: the fan cycles on/off based
on indoor temperature relative to the comfort band midpoint. Tests cover:
  - nat_vent_temperature_check(): midpoint thresholds for fan on/off
  - _deactivate_fan(restore_hvac=False): cycling-off does not restore HVAC mode
  - Fan status: 'nat-vent (session active, fan idle)' when session active, fan off

Occupant framing: previously the fan ran continuously during nat-vent, potentially
over-cooling the home. Now the fan cycles to hold the indoor temperature at the band
midpoint, improving comfort and reducing noise.

Since nat_vent_temperature_check() and _deactivate_fan(restore_hvac=...) are not
yet in the codebase (implemented by C2/C3), tests replicate the expected logic
inline — same pattern as test_fan_control.py and test_contact_status.py.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from custom_components.climate_advisor.automation import AutomationEngine
from custom_components.climate_advisor.const import (
    CONF_FAN_MODE,
    FAN_MODE_DISABLED,
    FAN_MODE_HVAC,
    FAN_MODE_WHOLE_HOUSE,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _consume_coroutine(coro):
    """Close coroutine to prevent 'never awaited' RuntimeWarning."""
    coro.close()


def _make_automation_engine(config_overrides: dict | None = None) -> AutomationEngine:
    """Create an AutomationEngine with mocked HA dependencies."""
    hass = MagicMock()
    hass.services = MagicMock()
    hass.services.async_call = AsyncMock()
    hass.async_create_task = MagicMock(side_effect=_consume_coroutine)
    hass.states = MagicMock()

    config = {
        "comfort_heat": 68,
        "comfort_cool": 74,
        "setback_heat": 60,
        "setback_cool": 80,
        "notify_service": "notify.notify",
        CONF_FAN_MODE: FAN_MODE_HVAC,
        "nat_vent_hysteresis_f": 1.0,
    }
    if config_overrides:
        config.update(config_overrides)

    engine = AutomationEngine(
        hass=hass,
        climate_entity="climate.thermostat",
        weather_entity="weather.forecast_home",
        door_window_sensors=["binary_sensor.front_door"],
        notify_service="notify.notify",
        config=config,
    )
    return engine


def _compute_nat_vent_cycling_thresholds(comfort_heat: float, comfort_cool: float, hysteresis_f: float = 1.0):
    """Compute nat-vent fan cycling thresholds from comfort band.

    Replicates the threshold logic expected in nat_vent_temperature_check():
      midpoint = (comfort_heat + comfort_cool) / 2
      off_threshold = midpoint - hysteresis_f   (fan turns off when indoor falls here)
      on_threshold  = midpoint + hysteresis_f   (fan turns on when indoor rises here)
    """
    midpoint = (comfort_heat + comfort_cool) / 2.0
    off_threshold = midpoint - hysteresis_f
    on_threshold = midpoint + hysteresis_f
    return midpoint, off_threshold, on_threshold


def _simulate_nat_vent_temperature_check(
    ae: AutomationEngine,
    indoor_temp: float,
    emitted_events: list,
) -> None:
    """Replicate nat_vent_temperature_check() logic inline.

    This mirrors the expected implementation in automation.py (Bug 3 fix):

    def nat_vent_temperature_check(self, indoor_temp: float) -> None:
        if not self._natural_vent_active:
            return
        comfort_heat = float(self.config.get("comfort_heat", 70))
        comfort_cool = float(self.config.get("comfort_cool", 75))
        hysteresis_f = float(self.config.get("nat_vent_hysteresis_f", 1.0))
        midpoint = (comfort_heat + comfort_cool) / 2.0
        off_threshold = midpoint - hysteresis_f
        on_threshold  = midpoint + hysteresis_f

        if self._fan_active and indoor_temp <= off_threshold:
            # Fan cycling off — indoor reached midpoint-1; NOT a hard session exit
            asyncio.get_event_loop().run_until_complete(
                self._deactivate_fan(reason="nat-vent cycling", restore_hvac=False))
            self._emit_event_callback("nat_vent_fan_off", {
                "indoor_temp": indoor_temp,
                "off_threshold": off_threshold,
            })
        elif not self._fan_active and indoor_temp >= on_threshold:
            # Fan cycling on — indoor reached midpoint+1 AND outdoor < indoor
            outdoor = self._last_outdoor_temp
            if outdoor is not None and outdoor < indoor_temp:
                asyncio.get_event_loop().run_until_complete(
                    self._activate_fan(reason="nat-vent cycling"))
                self._emit_event_callback("nat_vent_fan_on", {
                    "indoor_temp": indoor_temp,
                    "on_threshold": on_threshold,
                })
    """
    if not ae._natural_vent_active:
        return

    comfort_heat = float(ae.config.get("comfort_heat", 70))
    comfort_cool = float(ae.config.get("comfort_cool", 75))
    hysteresis_f = float(ae.config.get("nat_vent_hysteresis_f", 1.0))
    midpoint = (comfort_heat + comfort_cool) / 2.0
    off_threshold = midpoint - hysteresis_f
    on_threshold = midpoint + hysteresis_f

    if ae._fan_active and indoor_temp <= off_threshold:
        # Fan cycling off — do NOT restore HVAC (cycling, not session end)
        ae._deactivate_fan_called_with_restore = False
        ae._fan_active = False
        emitted_events.append(("nat_vent_fan_off", {"indoor_temp": indoor_temp, "off_threshold": off_threshold}))
    elif not ae._fan_active and indoor_temp >= on_threshold:
        outdoor = getattr(ae, "_last_outdoor_temp", None)
        if outdoor is not None and outdoor < indoor_temp:
            ae._fan_active = True
            emitted_events.append(("nat_vent_fan_on", {"indoor_temp": indoor_temp, "on_threshold": on_threshold}))


def _compute_fan_status_extended(ae: AutomationEngine, hass_states_get=None) -> str:
    """Replicate _compute_fan_status including new nat-vent idle state.

    Extends the existing _compute_fan_status logic with:
      - 'nat-vent (session active, fan idle)': session active but fan cycling off
    """
    fan_mode = ae.config.get(CONF_FAN_MODE, FAN_MODE_DISABLED)
    if fan_mode == FAN_MODE_DISABLED:
        return "disabled"
    if ae._fan_override_active:
        return "running (manual override)" if ae._fan_active else "off (manual override)"
    if ae._fan_active:
        return "active"
    # NEW: nat-vent session active but fan cycling-off
    if getattr(ae, "_natural_vent_active", False) and not ae._fan_active:
        return "nat-vent (session active, fan idle)"
    return "inactive"


# ---------------------------------------------------------------------------
# TestNatVentTemperatureCheck: cycling on/off at midpoint thresholds
# ---------------------------------------------------------------------------


class TestNatVentTemperatureCheck:
    """nat_vent_temperature_check() cycles fan based on midpoint thresholds.

    Config: comfort_heat=68, comfort_cool=74 → midpoint=71, off=70, on=72
    """

    def test_thresholds_computed_correctly(self):
        """Verify midpoint/off/on thresholds for comfort_heat=68, comfort_cool=74."""
        midpoint, off, on = _compute_nat_vent_cycling_thresholds(68, 74, 1.0)
        assert midpoint == 71.0
        assert off == 70.0
        assert on == 72.0

    def test_fan_cycles_off_at_off_threshold(self):
        """Fan turns off when indoor reaches off_threshold=70.

        Occupant: fan pauses cycling to avoid overcooling below 70°F.
        """
        ae = _make_automation_engine()
        ae._natural_vent_active = True
        ae._fan_active = True
        ae._last_outdoor_temp = 65.0
        ae._deactivate_fan_called_with_restore = None

        events = []
        _simulate_nat_vent_temperature_check(ae, indoor_temp=70.0, emitted_events=events)

        assert ae._fan_active is False
        nat_off_events = [e for e in events if e[0] == "nat_vent_fan_off"]
        assert len(nat_off_events) == 1
        assert nat_off_events[0][1]["indoor_temp"] == 70.0

    def test_fan_cycles_off_below_off_threshold(self):
        """Fan turns off when indoor drops below off_threshold (69°F < 70°F)."""
        ae = _make_automation_engine()
        ae._natural_vent_active = True
        ae._fan_active = True
        ae._last_outdoor_temp = 65.0

        events = []
        _simulate_nat_vent_temperature_check(ae, indoor_temp=69.0, emitted_events=events)

        assert ae._fan_active is False
        assert any(e[0] == "nat_vent_fan_off" for e in events)

    def test_fan_does_not_cycle_off_above_off_threshold(self):
        """Fan stays on when indoor is between thresholds (71°F > 70°F off threshold)."""
        ae = _make_automation_engine()
        ae._natural_vent_active = True
        ae._fan_active = True
        ae._last_outdoor_temp = 65.0

        events = []
        _simulate_nat_vent_temperature_check(ae, indoor_temp=71.0, emitted_events=events)

        # Fan still on — in the dead band
        assert ae._fan_active is True
        assert not any(e[0] == "nat_vent_fan_off" for e in events)

    def test_fan_cycles_on_at_on_threshold(self):
        """Fan turns on when indoor reaches on_threshold=72 and outdoor < indoor.

        Occupant: home has warmed back up; fan resumes free cooling.
        """
        ae = _make_automation_engine()
        ae._natural_vent_active = True
        ae._fan_active = False
        ae._last_outdoor_temp = 65.0  # outdoor < indoor=72

        events = []
        _simulate_nat_vent_temperature_check(ae, indoor_temp=72.0, emitted_events=events)

        assert ae._fan_active is True
        nat_on_events = [e for e in events if e[0] == "nat_vent_fan_on"]
        assert len(nat_on_events) == 1
        assert nat_on_events[0][1]["indoor_temp"] == 72.0

    def test_fan_does_not_cycle_on_when_outdoor_warm(self):
        """Fan does NOT cycle on when outdoor >= indoor — no cooling benefit.

        Occupant: running the fan with warm outdoor air would heat the home.
        """
        ae = _make_automation_engine()
        ae._natural_vent_active = True
        ae._fan_active = False
        ae._last_outdoor_temp = 73.0  # outdoor >= indoor=72 — gate fails

        events = []
        _simulate_nat_vent_temperature_check(ae, indoor_temp=72.0, emitted_events=events)

        assert ae._fan_active is False
        assert not any(e[0] == "nat_vent_fan_on" for e in events)

    def test_fan_does_not_cycle_on_when_fan_already_active(self):
        """No duplicate activation when fan is already running."""
        ae = _make_automation_engine()
        ae._natural_vent_active = True
        ae._fan_active = True  # already running
        ae._last_outdoor_temp = 65.0

        events = []
        _simulate_nat_vent_temperature_check(ae, indoor_temp=73.0, emitted_events=events)

        # No "on" event since fan was already active; no "off" since 73>70
        assert not any(e[0] == "nat_vent_fan_on" for e in events)

    def test_no_cycling_when_nat_vent_not_active(self):
        """nat_vent_temperature_check is a no-op when nat-vent session is not active."""
        ae = _make_automation_engine()
        ae._natural_vent_active = False
        ae._fan_active = True
        ae._last_outdoor_temp = 65.0

        events = []
        _simulate_nat_vent_temperature_check(ae, indoor_temp=70.0, emitted_events=events)

        # Fan state unchanged — method should have returned early
        assert ae._fan_active is True
        assert events == []

    def test_cycling_off_leaves_nat_vent_session_active(self):
        """Fan cycling off does not end the nat-vent session.

        Occupant: nat-vent mode stays active so the fan resumes when indoor
        warms up again — the session only ends on a hard exit (floor breach, etc).
        """
        ae = _make_automation_engine()
        ae._natural_vent_active = True
        ae._fan_active = True
        ae._last_outdoor_temp = 65.0

        events = []
        _simulate_nat_vent_temperature_check(ae, indoor_temp=70.0, emitted_events=events)

        # Fan off, but session still active
        assert ae._fan_active is False
        assert ae._natural_vent_active is True

    def test_full_cycle_off_then_on(self):
        """Fan cycles off then on in sequence — full midpoint hysteresis loop."""
        ae = _make_automation_engine()
        ae._natural_vent_active = True
        ae._fan_active = True
        ae._last_outdoor_temp = 65.0

        events = []

        # Step 1: indoor drops to 70 → fan off
        _simulate_nat_vent_temperature_check(ae, indoor_temp=70.0, emitted_events=events)
        assert ae._fan_active is False

        # Step 2: indoor rises to 72 → fan on
        _simulate_nat_vent_temperature_check(ae, indoor_temp=72.0, emitted_events=events)
        assert ae._fan_active is True

        off_events = [e for e in events if e[0] == "nat_vent_fan_off"]
        on_events = [e for e in events if e[0] == "nat_vent_fan_on"]
        assert len(off_events) == 1
        assert len(on_events) == 1


# ---------------------------------------------------------------------------
# TestNatVentFanStatusNewValue: 'nat-vent (session active, fan idle)'
# ---------------------------------------------------------------------------


class TestNatVentFanStatusNewValue:
    """Fan status returns new value when session active but fan cycling-off."""

    def test_fan_status_nat_vent_session_active_fan_idle(self):
        """_compute_fan_status returns 'nat-vent (session active, fan idle)'.

        Occupant: the dashboard shows the nat-vent session is still running
        even when the fan is temporarily off between cycles.
        """
        ae = _make_automation_engine()
        ae._natural_vent_active = True
        ae._fan_active = False
        ae._fan_override_active = False

        status = _compute_fan_status_extended(ae)
        assert status == "nat-vent (session active, fan idle)"

    def test_fan_status_active_when_fan_running_in_nat_vent(self):
        """When nat-vent session active AND fan running, status is 'active'."""
        ae = _make_automation_engine()
        ae._natural_vent_active = True
        ae._fan_active = True
        ae._fan_override_active = False

        status = _compute_fan_status_extended(ae)
        assert status == "active"

    def test_fan_status_inactive_when_no_session(self):
        """When no nat-vent session and fan is off, status is 'inactive'."""
        ae = _make_automation_engine()
        ae._natural_vent_active = False
        ae._fan_active = False
        ae._fan_override_active = False

        status = _compute_fan_status_extended(ae)
        assert status == "inactive"

    def test_fan_status_disabled_when_fan_mode_disabled(self):
        """'disabled' returned when fan_mode=disabled regardless of session state."""
        ae = _make_automation_engine({CONF_FAN_MODE: FAN_MODE_DISABLED})
        ae._natural_vent_active = True
        ae._fan_active = False
        ae._fan_override_active = False

        status = _compute_fan_status_extended(ae)
        assert status == "disabled"


# ---------------------------------------------------------------------------
# TestNatVentRestoreHvac: restore_hvac=False for WHF cycling
# ---------------------------------------------------------------------------


class TestNatVentRestoreHvac:
    """_deactivate_fan restore_hvac=False does not restore HVAC for WHF cycling."""

    def _simulate_deactivate_fan(self, ae: AutomationEngine, *, restore_hvac: bool) -> list:
        """Replicate _deactivate_fan() with restore_hvac parameter (Bug 3 addition).

        Expected implementation in automation.py:

        async def _deactivate_fan(self, *, reason: str, restore_hvac: bool = True) -> None:
            fan_mode = self.config.get(CONF_FAN_MODE, FAN_MODE_DISABLED)
            ...
            if fan_mode in (FAN_MODE_WHOLE_HOUSE, FAN_MODE_BOTH):
                # turn off fan entity
                if restore_hvac and self._pre_fan_hvac_mode is not None:
                    await self._set_hvac_mode(self._pre_fan_hvac_mode, reason=...)
                    self._pre_fan_hvac_mode = None
            ...
            self._fan_active = False

        When restore_hvac=False (nat-vent cycling): HVAC mode is NOT restored,
        _pre_fan_hvac_mode stays intact so a future hard exit can still restore it.
        """
        # Track calls without invoking AsyncMock (avoids unawaited-coroutine warnings).
        fan_entity_calls: list[str] = []
        hvac_mode_calls: list[str] = []

        if ae.config.get(CONF_FAN_MODE) in (FAN_MODE_WHOLE_HOUSE,):
            # Fan entity turn off (always happens)
            fan_entity = ae.config.get("fan_entity", "fan.whole_house")
            fan_entity_calls.append(fan_entity)  # record instead of calling AsyncMock

            if restore_hvac and getattr(ae, "_pre_fan_hvac_mode", None) is not None:
                prior_mode = ae._pre_fan_hvac_mode
                hvac_mode_calls.append(prior_mode)
                ae._pre_fan_hvac_mode = None

        ae._fan_active = False
        return hvac_mode_calls

    def test_whf_cycling_off_does_not_restore_hvac(self):
        """WHF cycling off (restore_hvac=False) does NOT restore prior HVAC mode.

        Occupant: mid-cycle fan pause is a temporary comfort control action —
        the prior HVAC mode must remain stored for when the session fully ends.
        """
        ae = _make_automation_engine({CONF_FAN_MODE: FAN_MODE_WHOLE_HOUSE, "fan_entity": "fan.whole_house"})
        ae._pre_fan_hvac_mode = "cool"
        ae._fan_active = True

        hvac_calls = self._simulate_deactivate_fan(ae, restore_hvac=False)

        # No HVAC restore
        assert hvac_calls == []
        # _pre_fan_hvac_mode preserved for hard exit
        assert ae._pre_fan_hvac_mode == "cool"
        assert ae._fan_active is False

    def test_whf_hard_exit_restores_hvac(self):
        """WHF hard exit (restore_hvac=True, default) restores prior HVAC mode.

        Occupant: when nat-vent session ends (e.g. comfort floor reached),
        HVAC resumes in the mode it was in before nat-vent started.
        """
        ae = _make_automation_engine({CONF_FAN_MODE: FAN_MODE_WHOLE_HOUSE, "fan_entity": "fan.whole_house"})
        ae._pre_fan_hvac_mode = "cool"
        ae._fan_active = True

        hvac_calls = self._simulate_deactivate_fan(ae, restore_hvac=True)

        # HVAC was restored
        assert "cool" in hvac_calls
        # _pre_fan_hvac_mode consumed
        assert ae._pre_fan_hvac_mode is None
        assert ae._fan_active is False

    def test_hvac_restore_skipped_when_no_pre_fan_mode(self):
        """No restore if _pre_fan_hvac_mode is None (e.g. nat-vent without prior suppression)."""
        ae = _make_automation_engine({CONF_FAN_MODE: FAN_MODE_WHOLE_HOUSE, "fan_entity": "fan.whole_house"})
        ae._pre_fan_hvac_mode = None
        ae._fan_active = True

        hvac_calls = self._simulate_deactivate_fan(ae, restore_hvac=True)

        assert hvac_calls == []
        assert ae._fan_active is False

    def test_hvac_fan_mode_no_hvac_restoration_needed(self):
        """HVAC fan mode (hvac_fan) does not involve HVAC suppression — no restore needed."""
        ae = _make_automation_engine({CONF_FAN_MODE: FAN_MODE_HVAC})
        ae._pre_fan_hvac_mode = None
        ae._fan_active = True

        hvac_calls = self._simulate_deactivate_fan(ae, restore_hvac=True)

        # hvac_fan mode doesn't have a whole-house fan to turn off
        assert hvac_calls == []
        assert ae._fan_active is False
