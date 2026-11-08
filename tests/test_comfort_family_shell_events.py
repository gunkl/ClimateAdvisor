"""End-to-end shell coverage for the comfort-family wiring in ``automation.py``
(Issue #827, rewritten for Issue #1066's nearest-edge arming) -- the contracts that
the pure leaf/FSM unit tests (``test_comfort_family_decision.py`` /
``test_comfort_family_fsm.py``) deliberately do NOT cover, because they live in the
shell rather than in the pure code:

1. **The ``comfort_family_switch_locked_out`` event contract**: the shell must turn the
   FSM's ``locked_out`` flag into an emitted event with the payload
   ``ai_skills_context.py``'s ``_render_comfort_family_switch_locked_out()`` reads
   (``candidate_family``, ``reason``).
2. **The ``comfort_family_switch`` event contract**: payload shape for
   ``_render_comfort_family_switch()`` (``resolved_family``, ``reason``,
   ``margin_applied_f``, ``indoor_f``, ``floor_f``, ``ceiling_f``, ``breach``).
3. **The ``_comfort_mode_family`` compatibility attribute**: the shell writes it so
   ``tools/sim_harness/outcomes.py``'s ``"comfort_family"`` assertion keeps working.
4. **The thermostat is the memory**: the current family is read from the live
   thermostat mode (or a CA command < 30 s old), never from stale bookkeeping.

All driven through the real ``_resolve_comfort_family_via_fsm()`` on a real
``AutomationEngine``, not a reimplementation of its logic (no-mirror-tests doctrine).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

from custom_components.climate_advisor.automation import AutomationEngine
from custom_components.climate_advisor.comfort_family_fsm import ComfortFamilyDwellState
from custom_components.climate_advisor.const import (
    CLIMATE_FEATURE_TARGET_TEMP_RANGE,
    DAY_TYPE_WARM,
)

_T0 = datetime(2026, 1, 1, 12, 0, 0)


def _consume_coroutine(coro):
    coro.close()


def _make_engine(*, indoor_temp: float, current_mode: str = "cool", **cfg) -> AutomationEngine:
    hass = MagicMock()
    hass.services = MagicMock()
    hass.services.async_call = AsyncMock()
    hass.async_create_task = MagicMock(side_effect=_consume_coroutine)
    hass.states = MagicMock()

    climate_state = MagicMock()
    climate_state.state = current_mode
    climate_state.attributes = {
        "hvac_modes": ["off", "heat", "cool"],
        "supported_features": CLIMATE_FEATURE_TARGET_TEMP_RANGE,
        "current_temperature": indoor_temp,
    }
    hass.states.get.return_value = climate_state

    config = {
        "comfort_heat": 68.0,
        "comfort_cool": 76.0,
        "setback_heat": 60.0,
        "setback_cool": 82.0,
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
    engine._manual_override_active = False
    engine._natural_vent_active = False
    engine._fan_active = False
    engine._fan_override_active = False
    engine.inputs.thermal = {}
    return engine


def _engine_wanting_a_heat_switch(*, dwell_since: datetime | None) -> tuple[AutomationEngine, list]:
    """Thermostat in COOL, indoor 69F on a 68/76 band: inside the heat arming margin (68 + 2),
    but not a breach -- so the leaf wants to switch to heat and ONLY the min-dwell timer can
    block it. ``dwell_since`` controls whether that timer is running."""
    engine = _make_engine(indoor_temp=69.0, current_mode="cool")
    engine._comfort_mode_family = "cooling"
    engine._comfort_family_dwell_state = ComfortFamilyDwellState(dwell_since=dwell_since)
    events: list = []
    engine._emit_event_callback = lambda name, payload: events.append((name, payload))
    return engine, events


class TestLockedOutEventContract:
    """The FSM's blocked-by-dwell-timer path must emit the event/payload the renderer expects."""

    def test_locked_out_emits_event_with_renderer_payload(self):
        # dwell clock moved 100s ago; min interval defaults to 600s -> blocked.
        engine, events = _engine_wanting_a_heat_switch(dwell_since=_T0 - timedelta(seconds=100))

        resolved = engine._resolve_comfort_family_via_fsm(
            "cool", floor=68.0, ceiling=76.0, now=_T0, day_type=DAY_TYPE_WARM
        )

        # The lockout held the thermostat's current family.
        assert resolved == "cool"
        assert len(events) == 1, f"expected exactly one emitted event, got {events}"
        name, payload = events[0]
        assert name == "comfort_family_switch_locked_out"
        assert payload["candidate_family"] == "heating"
        assert isinstance(payload["reason"], str) and payload["reason"]

    def test_renderer_accepts_the_emitted_payload(self):
        from custom_components.climate_advisor.ai_skills_context import (
            _render_comfort_family_switch_locked_out,
        )

        engine, events = _engine_wanting_a_heat_switch(dwell_since=_T0 - timedelta(seconds=100))
        engine._resolve_comfort_family_via_fsm("cool", floor=68.0, ceiling=76.0, now=_T0, day_type=DAY_TYPE_WARM)
        _name, payload = events[0]

        rendered = _render_comfort_family_switch_locked_out(payload, "F")
        assert isinstance(rendered, tuple) and len(rendered) == 2
        assert all(isinstance(part, str) and part for part in rendered)

    def test_not_locked_out_emits_switch_event_not_lockout(self):
        """Negative control: same state, dwell clock long expired -> the switch goes through."""
        engine, events = _engine_wanting_a_heat_switch(dwell_since=_T0 - timedelta(seconds=6000))

        resolved = engine._resolve_comfort_family_via_fsm(
            "cool", floor=68.0, ceiling=76.0, now=_T0, day_type=DAY_TYPE_WARM
        )

        assert resolved == "heat"
        assert [name for name, _payload in events] == ["comfort_family_switch"]

    def test_breach_ignores_the_dwell_lock(self):
        """Indoor below the floor is a breach: heat is armed immediately even 100 s after a switch."""
        engine, events = _engine_wanting_a_heat_switch(dwell_since=_T0 - timedelta(seconds=100))
        engine.hass.states.get.return_value.attributes["current_temperature"] = 66.0

        resolved = engine._resolve_comfort_family_via_fsm(
            "cool", floor=68.0, ceiling=76.0, now=_T0, day_type=DAY_TYPE_WARM
        )

        assert resolved == "heat"
        assert [name for name, _payload in events] == ["comfort_family_switch"]
        assert events[0][1]["breach"] is True


class TestComfortFamilySwitchEventContract:
    """A genuine heat/cool switch is persisted as an event with the Issue #1066 payload."""

    def test_switch_emits_event_with_renderer_payload(self):
        engine, events = _engine_wanting_a_heat_switch(dwell_since=_T0 - timedelta(seconds=6000))

        resolved = engine._resolve_comfort_family_via_fsm(
            "cool", floor=68.0, ceiling=76.0, now=_T0, day_type=DAY_TYPE_WARM
        )

        assert resolved == "heat"
        assert len(events) == 1, f"expected exactly one emitted event, got {events}"
        name, payload = events[0]
        assert name == "comfort_family_switch"
        assert payload["resolved_family"] == "heating"
        assert isinstance(payload["reason"], str) and payload["reason"]
        assert payload["margin_applied_f"] == 2.0
        assert payload["indoor_f"] == 69.0
        assert payload["floor_f"] == 68.0
        assert payload["ceiling_f"] == 76.0
        assert payload["breach"] is False

    def test_renderer_accepts_the_emitted_payload(self):
        from custom_components.climate_advisor.ai_skills_context import (
            _render_comfort_family_switch,
        )

        engine, events = _engine_wanting_a_heat_switch(dwell_since=_T0 - timedelta(seconds=6000))
        engine._resolve_comfort_family_via_fsm("cool", floor=68.0, ceiling=76.0, now=_T0, day_type=DAY_TYPE_WARM)
        _name, payload = events[0]

        rendered = _render_comfort_family_switch(payload, "F")
        assert isinstance(rendered, tuple) and len(rendered) == 2
        assert all(isinstance(part, str) and part for part in rendered)

    def test_hold_emits_no_event(self):
        """Thermostat already in the family the selector wants: nothing is emitted."""
        engine = _make_engine(indoor_temp=69.0, current_mode="heat")
        events: list = []
        engine._emit_event_callback = lambda name, payload: events.append((name, payload))

        resolved = engine._resolve_comfort_family_via_fsm(
            "cool", floor=68.0, ceiling=76.0, now=_T0, day_type=DAY_TYPE_WARM
        )

        assert resolved == "heat"
        assert events == []


class TestComfortModeFamilyCompatibilityAttribute:
    """The shell keeps writing `_comfort_mode_family` for tools/sim_harness/outcomes.py."""

    def test_switch_writes_the_compatibility_attribute(self):
        engine, _events = _engine_wanting_a_heat_switch(dwell_since=_T0 - timedelta(seconds=6000))
        assert engine._comfort_mode_family == "cooling"

        engine._resolve_comfort_family_via_fsm("cool", floor=68.0, ceiling=76.0, now=_T0, day_type=DAY_TYPE_WARM)

        assert engine._comfort_mode_family == "heating"

    def test_lockout_leaves_the_compatibility_attribute_untouched(self):
        engine, _events = _engine_wanting_a_heat_switch(dwell_since=_T0 - timedelta(seconds=100))

        engine._resolve_comfort_family_via_fsm("cool", floor=68.0, ceiling=76.0, now=_T0, day_type=DAY_TYPE_WARM)

        assert engine._comfort_mode_family == "cooling"


class TestThermostatIsTheMemory:
    """The current family is the thermostat's live mode, never stale bookkeeping."""

    def test_cool_day_mid_band_with_cool_thermostat_stays_cool(self):
        engine = _make_engine(indoor_temp=73.5, current_mode="cool")

        resolved = engine._resolve_comfort_family_via_fsm("cool", floor=70.0, ceiling=74.0, now=_T0, day_type="cool")

        assert resolved == "cool"
        assert engine._comfort_mode_family == "cooling"

    def test_stale_cooling_bookkeeping_does_not_override_a_heating_thermostat(self):
        """One of the out-of-scope `_arm_comfort_family("cooling")` writers left "cooling" behind;
        the thermostat is actually in heat and indoor is mid-band, so heat is kept."""
        engine = _make_engine(indoor_temp=72.0, current_mode="heat")
        engine._comfort_mode_family = "cooling"

        resolved = engine._resolve_comfort_family_via_fsm("heat", floor=68.0, ceiling=76.0, now=_T0, day_type="cool")

        assert resolved == "heat"

    def test_fresh_ca_command_wins_over_lagging_thermostat_state(self):
        """CA commanded heat 5 s ago but the entity still reads cool: the command is the memory."""
        engine = _make_engine(indoor_temp=72.0, current_mode="cool")
        engine._last_commanded_hvac_mode = "heat"
        engine._last_commanded_hvac_time = _T0 - timedelta(seconds=5)

        resolved = engine._resolve_comfort_family_via_fsm("cool", floor=68.0, ceiling=76.0, now=_T0, day_type="warm")

        assert resolved == "heat"

    def test_old_ca_command_is_ignored_in_favour_of_live_state(self):
        engine = _make_engine(indoor_temp=72.0, current_mode="cool")
        engine._last_commanded_hvac_mode = "heat"
        engine._last_commanded_hvac_time = _T0 - timedelta(seconds=300)

        resolved = engine._resolve_comfort_family_via_fsm("heat", floor=68.0, ceiling=76.0, now=_T0, day_type="warm")

        assert resolved == "cool"
