"""Issue #1232: Climate Advisor's own late setpoint echo is never read as the occupant.

Occupant story: a cloud thermostat reports a setpoint Climate Advisor wrote 30-120 s after the write. Read as the
person touching the dial it confirms a false manual override and pauses automation for the manual grace period
(1 h by default) -- and the planned remote-sensor loop (#1194) writes stepped nudges overnight, so the sleeper would
wake to a house that stopped following its plan. The person who REALLY changes the setpoint must still be caught.

These drive the REAL coordinator + REAL engine headlessly (``tools.sim_harness``): Climate Advisor writes through
``AutomationEngine._set_temperature`` and the thermostat's answer is delivered by hand at the chosen delay.
The harness's instant state feedback is switched off so the echo is exactly as late as the test says.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from custom_components.climate_advisor.classifier import DayClassification
from custom_components.climate_advisor.learning import DailyRecord
from tools.sim_harness._loop import run_coro
from tools.sim_harness.build_coordinator import build_headless_coordinator
from tools.sim_harness.fake_hass import FakeEvent, FakeState
from tools.sim_harness.ha_stubs import install_ha_stubs

install_ha_stubs()

FAN_MODES = ["whole_house_fan", "hvac_fan"]
_FAN_CONFIG = {
    "whole_house_fan": {"fan_mode": "whole_house_fan", "fan_entity": "fan.whf"},
    "hvac_fan": {"fan_mode": "hvac_fan"},
}


class _House:
    """A headless coordinator + engine, a thermostat at ``initial`` and CA's classification (heat)."""

    def __init__(
        self,
        fan_mode: str,
        *,
        unit: str = "fahrenheit",
        initial: float = 70.0,
        state: str = "heat",
        attrs: dict | None = None,
    ) -> None:
        self.unit = unit
        self.attrs = {
            "current_temperature": 69.0,
            "temperature": initial,
            "hvac_action": "idle",
            "fan_mode": "auto",
            "fan_modes": ["auto", "on"],
            "hvac_modes": ["off", "heat", "cool"],
        }
        self.attrs.update(attrs or {})
        cfg = {"comfort_heat": 70, "comfort_cool": 78, "temp_unit": unit, **_FAN_CONFIG[fan_mode]}
        self.co, self.fh, self.sch, self.ev = build_headless_coordinator(
            config=cfg, climate_state=state, climate_attributes=dict(self.attrs), skip_startup_coalesce=True
        )
        self.ce = self.co.config["climate_entity"]
        self.ae = self.co.automation_engine
        self.co._current_classification = DayClassification(
            day_type="cold",
            trend_direction="stable",
            trend_magnitude=0,
            today_high=40,
            today_low=25,
            tomorrow_high=40,
            tomorrow_low=25,
            hvac_mode="heat",
        )
        self.co._today_record = DailyRecord(date="2024-01-15", day_type="cold", trend_direction="stable")
        # Real thermostats answer later than the call: no instant feedback, the test delivers the echo.
        self.fh.services._apply_state_feedback = lambda *a, **k: None
        self.state = FakeState(state=state, attributes=dict(self.attrs))
        self.fh.states.set(self.ce, self.state)

    def ca_writes(self, temperature_f: float, mode: str = "heat") -> None:
        """Climate Advisor's own setpoint write (internal °F, converted to the thermostat's unit)."""
        run_coro(self.ae._set_temperature(temperature_f, reason="test write", mode=mode))

    def wait(self, seconds: float) -> None:
        self.sch.advance_by(int(timedelta(seconds=seconds).total_seconds()))

    def thermostat_reports(self, state: str | None = None, context=None, **attrs) -> None:
        """The thermostat reports a new state (the old one is whatever it reported last)."""
        old = self.state
        new_attrs = dict(old.attributes)
        new_attrs.update(attrs)
        new = FakeState(state=state or old.state, attributes=new_attrs)
        self.fh.states.set(self.ce, new)
        self.state = new
        event = FakeEvent(
            event_type="state_changed",
            data={"entity_id": self.ce, "old_state": old, "new_state": new},
            context=context,
        )
        run_coro(self.co._async_thermostat_changed(event))

    @property
    def overrides(self) -> int:
        return self.co._today_record.manual_overrides

    @property
    def override_state(self) -> tuple[bool, bool]:
        return bool(self.ae._manual_override_active), bool(self.ae._override_confirm_pending)

    def ca_context(self):
        calls = [a for a in self.fh.action_log if a["service"] == "set_temperature"]
        return calls[-1]["context"]


def _run(fan_mode: str, **kw):
    house = _House(fan_mode, **kw)
    return house, house.sch.installed()


# --- the occupant is NOT blamed for CA's own late echo -----------------------------------------------------------


@pytest.mark.parametrize("fan_mode", FAN_MODES)
@pytest.mark.parametrize("delay_s", [45, 110, 179])
def test_ca_write_echoed_late_is_not_an_override(fan_mode, delay_s):
    house, clock = _run(fan_mode)
    with clock:
        house.ca_writes(72.0)
        house.wait(delay_s)
        house.thermostat_reports(temperature=72.0)
    assert house.overrides == 0
    assert house.override_state == (False, False)


@pytest.mark.parametrize("fan_mode", FAN_MODES)
def test_late_echo_that_also_carries_the_mode_is_not_an_override(fan_mode):
    """CA's write bundles hvac_mode + temperature; a cool->heat thermostat reports both at 110 s."""
    house, clock = _run(fan_mode, state="cool")
    with clock:
        house.ca_writes(72.0, mode="heat")
        house.wait(110)
        house.thermostat_reports(state="heat", temperature=72.0)
    assert house.overrides == 0
    assert house.override_state == (False, False)


# --- the occupant who really changes it is still caught -----------------------------------------------------------


@pytest.mark.parametrize("fan_mode", FAN_MODES)
def test_different_value_at_45s_is_still_an_override(fan_mode):
    house, clock = _run(fan_mode)
    with clock:
        house.ca_writes(72.0)
        house.wait(45)
        house.thermostat_reports(temperature=75.0)  # not what CA wrote: the occupant
    assert house.overrides == 1
    assert any(house.override_state)


@pytest.mark.parametrize("fan_mode", FAN_MODES)
@pytest.mark.parametrize("delay_s", [181, 600])
def test_equal_value_after_the_window_is_an_override(fan_mode, delay_s):
    house, clock = _run(fan_mode)
    with clock:
        house.ca_writes(72.0)
        house.wait(delay_s)
        house.thermostat_reports(temperature=72.0)
    assert house.overrides == 1
    assert any(house.override_state)


@pytest.mark.parametrize("fan_mode", FAN_MODES)
def test_echo_is_consumed_so_a_second_identical_change_is_the_occupant(fan_mode):
    """Cleared on echo arrival: after the echo, the occupant dialling back to the same value is real."""
    house, clock = _run(fan_mode)
    with clock:
        house.ca_writes(72.0)
        house.wait(60)
        house.thermostat_reports(temperature=72.0)  # CA's echo
        house.wait(20)
        house.thermostat_reports(temperature=74.0)  # occupant moves it
        house.thermostat_reports(temperature=72.0)  # ...and back to CA's old value, memory is empty
    assert house.overrides >= 1


# --- heat_cool dual setpoints -------------------------------------------------------------------------------------


def _band_house(fan_mode, **kw):
    return _House(fan_mode, state="heat_cool", attrs={"target_temp_low": 68.0, "target_temp_high": 76.0}, **kw)


@pytest.mark.parametrize("fan_mode", FAN_MODES)
def test_heat_cool_echo_of_both_setpoints_is_not_an_override(fan_mode):
    """A band write (low and high) echoed together at 110 s."""
    house = _band_house(fan_mode)
    with house.sch.installed():
        run_coro(
            house.ae._call_climate(
                "set_temperature",
                {"entity_id": house.ce, "target_temp_low": 69.0, "target_temp_high": 77.0},
                "band",
            )
        )
        house.wait(110)
        house.thermostat_reports(target_temp_low=69.0, target_temp_high=77.0)
    assert house.overrides == 0


@pytest.mark.parametrize("fan_mode", FAN_MODES)
def test_heat_cool_one_echoed_one_changed_is_an_override(fan_mode):
    """Low is CA's echo; high moved to a value CA never wrote: the occupant."""
    house = _band_house(fan_mode)
    with house.sch.installed():
        run_coro(
            house.ae._call_climate(
                "set_temperature",
                {"entity_id": house.ce, "target_temp_low": 69.0, "target_temp_high": 77.0},
                "band",
            )
        )
        house.wait(50)
        house.thermostat_reports(target_temp_low=69.0, target_temp_high=80.0)
    assert house.overrides == 1


@pytest.mark.parametrize("fan_mode", FAN_MODES)
def test_heat_cool_low_echo_alone_is_not_an_override(fan_mode):
    """The thermostat reports the low edge first, high later (two events); each is CA's own."""
    house = _band_house(fan_mode)
    with house.sch.installed():
        run_coro(
            house.ae._call_climate(
                "set_temperature",
                {"entity_id": house.ce, "target_temp_low": 69.0, "target_temp_high": 77.0},
                "band",
            )
        )
        house.wait(100)
        house.thermostat_reports(target_temp_low=69.0)
        house.wait(20)
        house.thermostat_reports(target_temp_high=77.0)
    assert house.overrides == 0


@pytest.mark.parametrize("fan_mode", FAN_MODES)
def test_heat_cool_single_temperature_write_reported_on_a_band_edge_is_not_an_override(fan_mode):
    """CA writes one ``temperature``; a band-mode thermostat reports it on the high edge at 90 s."""
    house = _band_house(fan_mode)
    with house.sch.installed():
        house.ca_writes(77.0, mode="cool")
        house.wait(90)
        house.thermostat_reports(target_temp_high=77.0)
    assert house.overrides == 0


# --- °C / °F rounding ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("fan_mode", FAN_MODES)
@pytest.mark.parametrize("reported_c", [22.0, 22.5])
def test_celsius_half_degree_thermostat_rounding_is_still_the_echo(fan_mode, reported_c):
    """72 F is 22.22 C; a 0.5 C thermostat reports 22.0 or 22.5 -- both are CA's write, whichever way it rounds."""
    house = _House(fan_mode, unit="celsius", initial=21.0, attrs={"target_temp_step": 0.5})
    with house.sch.installed():
        house.ca_writes(72.0)
        house.wait(100)
        house.thermostat_reports(temperature=reported_c)
    assert house.overrides == 0


@pytest.mark.parametrize("fan_mode", FAN_MODES)
def test_celsius_a_different_step_value_is_the_occupant(fan_mode):
    """Rounded echo of 22.22 C is 22.0/22.5; 23.0 C is a different setpoint."""
    house = _House(fan_mode, unit="celsius", initial=21.0, attrs={"target_temp_step": 0.5})
    with house.sch.installed():
        house.ca_writes(72.0)
        house.wait(100)
        house.thermostat_reports(temperature=23.0)
    assert house.overrides == 1


@pytest.mark.parametrize("fan_mode", FAN_MODES)
def test_celsius_on_grid_write_must_come_back_exactly(fan_mode):
    """CA writes an on-grid 22.5 C (72.5 F); the occupant's one-step tweak to 23.0 C is not the echo."""
    house = _House(fan_mode, unit="celsius", initial=21.0, attrs={"target_temp_step": 0.5})
    with house.sch.installed():
        house.ca_writes(72.5)  # 22.5 C
        house.wait(100)
        house.thermostat_reports(temperature=23.0)
    assert house.overrides == 1


@pytest.mark.parametrize("fan_mode", FAN_MODES)
def test_fahrenheit_whole_degree_rounding_is_still_the_echo(fan_mode):
    """CA writes 72.4 F; a whole-degree thermostat reports 72."""
    house = _House(fan_mode)
    with house.sch.installed():
        house.ca_writes(72.4)
        house.wait(100)
        house.thermostat_reports(temperature=72.0)
    assert house.overrides == 0


# --- several writes in flight -------------------------------------------------------------------------------------


@pytest.mark.parametrize("fan_mode", FAN_MODES)
def test_two_writes_in_flight_are_each_recognised(fan_mode):
    """#1194 nudges: 71 at t0, 72 at t+10 s, the thermostat answers 71 at 60 s and 72 at 80 s (in order)."""
    house, clock = _run(fan_mode)
    with clock:
        house.ca_writes(71.0)
        house.wait(10)
        house.ca_writes(72.0)
        house.wait(50)
        house.thermostat_reports(temperature=71.0)
        house.wait(20)
        house.thermostat_reports(temperature=72.0)
    assert house.overrides == 0
    assert house.override_state == (False, False)


@pytest.mark.parametrize("fan_mode", FAN_MODES)
def test_occupant_dialling_back_to_a_superseded_ca_value_is_detected(fan_mode):
    """CA writes 72, then 74; the thermostat reports only 74; the occupant dials 72 at t=120 s: the occupant.

    Before #1232 D3 the superseded 72 stayed remembered for 180 s and this real change was swallowed as CA's echo.
    """
    house, clock = _run(fan_mode)
    with clock:
        house.ca_writes(72.0)
        house.wait(60)
        house.ca_writes(74.0)
        house.wait(30)
        house.thermostat_reports(temperature=74.0)  # CA's newest write; the thermostat never reported 72
        assert house.overrides == 0
        house.wait(30)
        house.thermostat_reports(temperature=72.0)  # the occupant
    assert house.overrides == 1
    assert any(house.override_state)


@pytest.mark.parametrize("fan_mode", FAN_MODES)
def test_out_of_order_older_echo_is_judged_as_the_occupant(fan_mode):
    """ACCEPTED TRADE-OFF -- the owner's choice (2026-11-02, #1232 D3), not a bug.

    CA writes 71 then 72; the thermostat reports 72 first and the older 71 afterwards. Recognising 72 drops the
    superseded 71 (so an occupant dialling back to 71 is caught, see the test above), so the late, out-of-order
    71 is counted as the occupant. Reverse this test only with a new owner decision.
    """
    house, clock = _run(fan_mode)
    with clock:
        house.ca_writes(71.0)
        house.wait(10)
        house.ca_writes(72.0)
        house.wait(50)
        house.thermostat_reports(temperature=72.0)
        assert house.overrides == 0
        house.wait(20)
        house.thermostat_reports(temperature=71.0)
    assert house.overrides == 1


@pytest.mark.parametrize("fan_mode", FAN_MODES)
def test_write_in_flight_does_not_hide_a_third_value(fan_mode):
    house, clock = _run(fan_mode)
    with clock:
        house.ca_writes(71.0)
        house.wait(10)
        house.ca_writes(72.0)
        house.wait(60)
        house.thermostat_reports(temperature=75.0)
    assert house.overrides == 1


# --- HA Context ---------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("fan_mode", FAN_MODES)
def test_context_match_wins_over_a_different_rounding(fan_mode):
    """The state change carries CA's own Context id: it is CA's, whatever rounded value the thermostat shows."""
    house, clock = _run(fan_mode)
    with clock:
        house.ca_writes(72.0)
        ctx = house.ca_context()
        house.wait(100)
        house.thermostat_reports(context=ctx, temperature=73.0)  # a coarse thermostat rounded 72 up a whole step
    assert house.overrides == 0


@pytest.mark.parametrize("fan_mode", FAN_MODES)
def test_context_match_after_the_window_is_not_trusted(fan_mode):
    house, clock = _run(fan_mode)
    with clock:
        house.ca_writes(72.0)
        ctx = house.ca_context()
        house.wait(400)
        house.thermostat_reports(context=ctx, temperature=72.0)
    assert house.overrides == 1


# --- the occupant sets the SAME value CA just wrote ---------------------------------------------------------------


@pytest.mark.parametrize("fan_mode", FAN_MODES)
def test_occupant_sets_the_same_value_without_a_context_is_treated_as_the_echo(fan_mode):
    """Documented behaviour: by value alone the two are indistinguishable. The person asked for exactly the
    setpoint the plan already holds, so there is nothing to override."""
    house, clock = _run(fan_mode)
    with clock:
        house.ca_writes(72.0)
        house.wait(60)
        house.thermostat_reports(temperature=72.0)
    assert house.overrides == 0


@pytest.mark.parametrize("fan_mode", FAN_MODES)
def test_occupant_with_a_user_context_is_never_the_echo_even_at_the_same_value(fan_mode):
    """Context separates them when HA provides it: a change made by a signed-in person (user_id set) is theirs."""
    from homeassistant.core import Context

    house, clock = _run(fan_mode)
    with clock:
        house.ca_writes(72.0)
        house.wait(60)
        house.thermostat_reports(context=Context(user_id="user-123"), temperature=72.0)
    assert house.overrides == 1
