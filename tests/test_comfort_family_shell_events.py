"""End-to-end shell coverage for Issue #827's comfort-family FSM wiring in
``automation.py`` — specifically the two contracts that the FSM's own unit tests
(``test_comfort_family_fsm.py``) deliberately do NOT cover, because they live in the
shell rather than in the pure FSM:

1. **The ``comfort_family_switch_locked_out`` event contract** (Design §2 preserved
   contract). ``test_comfort_family_fsm.py`` only asserts that ``transition()`` sets
   ``ComfortFamilyTransition.locked_out``; nothing proved the shell actually turns
   that flag into an emitted event with the payload
   ``ai_skills_context.py``'s ``_render_comfort_family_switch_locked_out()`` reads
   (``candidate_family``, ``reason``). The plan's own round-2 review flagged exactly
   this: without such a test the contract "would otherwise silently go dead with no
   test currently forcing the gap to surface". This file forces it.

2. **The ``_comfort_mode_family`` compatibility attribute** (Design §2) — that the FSM
   path writes it, so ``tools/sim_harness/outcomes.py``'s ``"comfort_family"``
   assertion type keeps working.

Both are driven through the real ``_resolve_comfort_family_via_fsm()`` on a real
``AutomationEngine``, not a reimplementation of its logic (CLAUDE.md's no-mirror-tests
doctrine).
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


def _make_engine(*, indoor_temp: float, **cfg) -> AutomationEngine:
    hass = MagicMock()
    hass.services = MagicMock()
    hass.services.async_call = AsyncMock()
    hass.async_create_task = MagicMock(side_effect=_consume_coroutine)
    hass.states = MagicMock()

    climate_state = MagicMock()
    climate_state.state = "cool"
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
    return AutomationEngine(
        hass=hass,
        climate_entity="climate.thermostat",
        weather_entity="weather.forecast_home",
        door_window_sensors=[],
        notify_service=config["notify_service"],
        config=config,
    )


def _engine_wanting_a_heat_escalation(*, dwell_since: datetime | None) -> tuple[AutomationEngine, list]:
    """An engine in the exact state where the leaf wants to ESCALATE to heating:
    a warm day (native=cooling, so the heating direction is against-grain with a
    2.0F deadband), indoor 8F below the floor (well past that deadband), and the
    heat candidate already sustain-confirmed. ``dwell_since`` controls whether the
    FSM's min-dwell timer blocks that escalation.
    """
    engine = _make_engine(indoor_temp=60.0, comfort_deadband_warm_f=2.0)
    engine._manual_override_active = False
    engine._natural_vent_active = False
    engine._fan_active = False
    engine._fan_override_active = False
    engine._thermal_model = {}
    engine._comfort_mode_family = "cooling"
    engine._comfort_family_dwell_state = ComfortFamilyDwellState(
        dwell_since=dwell_since,
        is_against_grain=False,
        # Already a sustained heat candidate: the sustain-confirm gate is cleared,
        # so the only thing that can still block the switch is the min-dwell timer.
        heat_candidate_since=_T0 - timedelta(seconds=3600),
        heat_candidate_raw=True,
    )

    events: list = []
    engine._emit_event_callback = lambda name, payload: events.append((name, payload))
    return engine, events


class TestLockedOutEventContract:
    """Design §2: the FSM's blocked-by-dwell-timer path must emit the same event
    name/payload ai_skills_context.py's renderer expects."""

    def test_locked_out_emits_event_with_renderer_payload(self):
        # dwell clock moved 100s ago; min interval defaults to 600s -> blocked.
        engine, events = _engine_wanting_a_heat_escalation(dwell_since=_T0 - timedelta(seconds=100))

        resolved = engine._resolve_comfort_family_via_fsm(
            "cool", floor=68.0, ceiling=76.0, now=_T0, day_type=DAY_TYPE_WARM
        )

        # The lockout held the prior family — the caller keeps its cool day_mode.
        assert resolved == "cool"

        assert len(events) == 1, f"expected exactly one emitted event, got {events}"
        name, payload = events[0]
        assert name == "comfort_family_switch_locked_out"
        # The two keys _render_comfort_family_switch_locked_out() reads.
        assert payload["candidate_family"] == "heating"
        assert isinstance(payload["reason"], str) and payload["reason"]

    def test_renderer_accepts_the_emitted_payload(self):
        """Round-trips the real emitted payload through the real AI-skills renderer,
        so a payload-shape drift on either side fails here rather than silently
        producing a broken digest line."""
        from custom_components.climate_advisor.ai_skills_context import (
            _render_comfort_family_switch_locked_out,
        )

        engine, events = _engine_wanting_a_heat_escalation(dwell_since=_T0 - timedelta(seconds=100))
        engine._resolve_comfort_family_via_fsm("cool", floor=68.0, ceiling=76.0, now=_T0, day_type=DAY_TYPE_WARM)
        _name, payload = events[0]

        rendered = _render_comfort_family_switch_locked_out(payload, "F")
        assert isinstance(rendered, tuple) and len(rendered) == 2
        assert all(isinstance(part, str) and part for part in rendered)

    def test_not_locked_out_emits_switch_event_not_lockout(self):
        """Negative control: same engine state, dwell clock long expired -> the
        switch goes through, no lockout event (a comfort_family_switch event
        instead — see TestComfortFamilySwitchEventContract), and the compatibility
        attribute follows."""
        engine, events = _engine_wanting_a_heat_escalation(dwell_since=_T0 - timedelta(seconds=6000))

        resolved = engine._resolve_comfort_family_via_fsm(
            "cool", floor=68.0, ceiling=76.0, now=_T0, day_type=DAY_TYPE_WARM
        )

        assert resolved == "heat"
        assert [name for name, _payload in events] == ["comfort_family_switch"]


class TestComfortFamilySwitchEventContract:
    """Issue #843 follow-up: an actual heat/cool family switch was previously
    logged only to HA core logs (invisible to the briefing/investigator/activity
    log). This class forces the shell to emit a persisted event with the payload
    ai_skills_context.py's _render_comfort_family_switch() reads, including the
    values (deadband_applied_f, minutes_since_*_ended) needed to tell a
    recency-gated immediate switch apart from a genuine full-breach escalation."""

    def test_switch_emits_event_with_renderer_payload(self):
        engine, events = _engine_wanting_a_heat_escalation(dwell_since=_T0 - timedelta(seconds=6000))

        resolved = engine._resolve_comfort_family_via_fsm(
            "cool", floor=68.0, ceiling=76.0, now=_T0, day_type=DAY_TYPE_WARM
        )

        assert resolved == "heat"
        assert len(events) == 1, f"expected exactly one emitted event, got {events}"
        name, payload = events[0]
        assert name == "comfort_family_switch"
        assert payload["resolved_family"] == "heating"
        assert isinstance(payload["reason"], str) and payload["reason"]
        assert "deadband_applied_f" in payload
        assert "minutes_since_cooling_ended" in payload
        assert "minutes_since_heating_ended" in payload

    def test_renderer_accepts_the_emitted_payload(self):
        """Round-trips the real emitted payload through the real AI-skills renderer,
        so a payload-shape drift on either side fails here rather than silently
        producing a broken digest line."""
        from custom_components.climate_advisor.ai_skills_context import (
            _render_comfort_family_switch,
        )

        engine, events = _engine_wanting_a_heat_escalation(dwell_since=_T0 - timedelta(seconds=6000))
        engine._resolve_comfort_family_via_fsm("cool", floor=68.0, ceiling=76.0, now=_T0, day_type=DAY_TYPE_WARM)
        _name, payload = events[0]

        rendered = _render_comfort_family_switch(payload, "F")
        assert isinstance(rendered, tuple) and len(rendered) == 2
        assert all(isinstance(part, str) and part for part in rendered)


class TestComfortModeFamilyCompatibilityAttribute:
    """Design §2: the FSM path keeps writing `_comfort_mode_family`, which
    tools/sim_harness/outcomes.py's "comfort_family" assertion type reads."""

    def test_escalation_writes_the_compatibility_attribute(self):
        engine, _events = _engine_wanting_a_heat_escalation(dwell_since=_T0 - timedelta(seconds=6000))
        assert engine._comfort_mode_family == "cooling"

        engine._resolve_comfort_family_via_fsm("cool", floor=68.0, ceiling=76.0, now=_T0, day_type=DAY_TYPE_WARM)

        assert engine._comfort_mode_family == "heating"

    def test_lockout_leaves_the_compatibility_attribute_untouched(self):
        engine, _events = _engine_wanting_a_heat_escalation(dwell_since=_T0 - timedelta(seconds=100))

        engine._resolve_comfort_family_via_fsm("cool", floor=68.0, ceiling=76.0, now=_T0, day_type=DAY_TYPE_WARM)

        assert engine._comfort_mode_family == "cooling"


class TestDayTypeNeverOverridesTheClassifier:
    """Issue #827 Verification regression: `day_type` scales the deadbands; it must
    never by itself move the family away from what the classifier chose.

    Reproduces golden scenario `override_self_resolve_transient`'s conditions
    (day_type="cool" while the classifier's hvac_mode is "cool", indoor squarely
    mid-band). Before the fix this returned "heat" — silently rewriting the
    classifier's cool-mode decision into a furnace command on a comfortable home.
    """

    def test_cool_day_with_cool_classification_and_no_breach_stays_cool(self):
        engine = _make_engine(indoor_temp=73.5)
        engine._manual_override_active = False
        engine._natural_vent_active = False
        engine._fan_active = False
        engine._fan_override_active = False
        engine._thermal_model = {}
        engine._comfort_mode_family = None
        engine._comfort_family_dwell_state = None

        resolved = engine._resolve_comfort_family_via_fsm("cool", floor=70.0, ceiling=74.0, now=_T0, day_type="cool")

        assert resolved == "cool"
        assert engine._comfort_mode_family == "cooling"

    def test_stale_cooling_bookkeeping_does_not_override_a_heat_classification(self):
        """One of the 7 out-of-scope `_arm_comfort_family("cooling")` writers
        (nat-vent/WHF/economizer) left "cooling" behind with no genuine escalation
        on record. The classifier now says heat and indoor is mid-band — the shell
        must seed from the classifier, not from that stale attribute."""
        engine = _make_engine(indoor_temp=72.0)
        engine._manual_override_active = False
        engine._natural_vent_active = False
        engine._fan_active = False
        engine._fan_override_active = False
        engine._thermal_model = {}
        engine._comfort_mode_family = "cooling"
        engine._comfort_family_dwell_state = ComfortFamilyDwellState(is_against_grain=False)

        resolved = engine._resolve_comfort_family_via_fsm("heat", floor=68.0, ceiling=76.0, now=_T0, day_type="cool")

        assert resolved == "heat"
