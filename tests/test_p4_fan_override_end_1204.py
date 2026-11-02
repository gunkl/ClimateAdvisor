"""Issue #1204 (owner decisions A2 and A3, 2026-11-01): how a fan press or a rule-2 fan stop ends an override.

Occupant stories:

* hvac_fan: you lower the setpoint, then switch the thermostat's blower On a minute later. Climate Advisor keeps your
  setpoint until the blower's pause ends (decision A2: protected until the later of the two pause ends). The household
  promise checker (P4) used to report "ended your manual thermostat change 14 min early" here. It must not, and it
  must still report Climate Advisor changing your setpoint inside that time.
* whole_house_fan: you press the fan on, then set the thermostat to Cool. Rule 2 stops the older fan for the Cool.
  The fan is off, so its override must end with the stop (decision A3); the fan card used to keep saying
  "off (manual override)" for hours.

Test-infrastructure rule 8 (why the old journal semantics were WRONG, not just inconvenient): the journal closed the
thermostat episode as ``superseded_unreplaced`` the moment a blower grace replaced its grace, although production
keeps that setpoint protected (``_manual_override_active`` stays True) until the replacing grace ends. That reported
a fault that did not happen and, worse, stopped P4 guarding the setpoint for the rest of the blower's pause, so a
real reversal there went unreported. The episode now stays open only when the ENGINE says the request continues
(``continues: True``, the #1205 contract) on a ``superseded`` cancel; a replacement the engine does not vouch for
(an automation grace, a whole-house-fan press that rule 2 lets win) still closes it as ``superseded_unreplaced``, and a
shorter replacement is reported as ``grace_expired_early``. That early end is detected on the ``override_cleared``
(reason ``grace_expired``) the engine emits BEFORE ``grace_expired``: checked only on ``grace_expired``, it was dead in
production's event order (the clear had already closed the episode as an on-time ``grace_expired``), so a real early
end went unreported; ``TestJournalReadsTheEnginesOwnEventOrder`` feeds the real engine's stream to the journal so a
test-only order cannot hide that again. Could this let a real failure pass silently? Only if the
engine claimed ``continues`` while dropping the setpoint, which the engine test below pins to the one case where
production keeps it (a fan-override grace replacing the grace while the thermostat change is still protected).
"""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

if "homeassistant" not in sys.modules:
    from conftest import install_ha_stubs

    install_ha_stubs()

from custom_components.climate_advisor.automation import AutomationEngine, FanCommandResult  # noqa: E402
from custom_components.climate_advisor.const import (  # noqa: E402
    CONF_FAN_ENTITY,
    CONF_FAN_MODE,
    FAN_MODE_HVAC,
    FAN_MODE_WHOLE_HOUSE,
)
from custom_components.climate_advisor.invariant_watchdog import (  # noqa: E402
    SCOPE_TEST,
    PromiseConfig,
    run_promise_checks,
)
from custom_components.climate_advisor.promise_journal import (  # noqa: E402
    Command,
    PromiseInput,
    Reading,
    derive_episodes,
    mark_from_event,
)

T0 = datetime(2026, 7, 15, 14, 0, tzinfo=UTC)


def at(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


def rd(minutes: float, **kw) -> Reading:
    base = {"hvac_mode": "cool", "hvac_action": "cooling", "indoor_f": 76.0, "setpoint_f": 72.0, "fan_on": False}
    base.update(kw)
    return Reading(ts=at(minutes), **base)


def events(*items):
    return [mark_from_event(at(m), t, p) for m, t, p in items]


def make_input(readings=(), commands=(), marks=(), now=None) -> PromiseInput:
    readings = tuple(sorted(readings, key=lambda r: r.ts))
    marks = tuple(sorted(marks, key=lambda m: m.ts))
    return PromiseInput(
        now=now or at(60),
        latest=readings[-1] if readings else None,
        readings=readings,
        commands=tuple(commands),
        marks=marks,
        episodes=derive_episodes(marks, readings),
        observe_notices=(),
    )


def p4(inp: PromiseInput) -> list:
    return [h for h in run_promise_checks(inp, PromiseConfig(), scope=SCOPE_TEST) if h.promise == "override_respected"]


_READINGS = [rd(-1, setpoint_f=75.0), rd(0, setpoint_f=72.0)]


def _blower_after_setpoint(*, own_s=900, fan_s=900, continues=True, extra=()):
    """The #1204 item-1 event stream (generated seed 102000023): setpoint change, blower press 1.5 min later."""
    return events(
        (0, "override_detected", {"detected_mode": "cool"}),
        (0.5, "grace_started", {"source": "manual", "trigger": "override_confirmed", "duration_seconds": own_s}),
        (2, "fan_manual_override", {"fan_before": "auto", "fan_after": "on"}),
        (
            2,
            "grace_cancelled",
            {
                "reason": "superseded",
                "trigger": "override_confirmed",
                "protects_override": True,
                "continues": continues,
            },
        ),
        (2, "grace_started", {"source": "manual", "trigger": "fan_manual_override", "duration_seconds": fan_s}),
        # Production order (``_on_grace_expired()``: the override clears, then the grace expiry is announced),
        # pinned against the real engine by ``TestJournalReadsTheEnginesOwnEventOrder``.
        (2 + fan_s / 60, "override_cleared", {"was_mode": "cool", "reason": "grace_expired", "continues": False}),
        (2 + fan_s / 60, "grace_expired", {"source": "manual", "re_paused": False}),
        *extra,
    )


class TestJournalKeepsTheSetpointThroughTheBlowerPause:
    def test_blower_press_is_not_reported_as_ending_the_setpoint_early(self):
        inp = make_input(_READINGS, (), _blower_after_setpoint())
        thermostat = next(e for e in inp.episodes if e.target == "thermostat")
        assert thermostat.end_reason == "grace_expired"
        assert thermostat.end_ts == at(17) and thermostat.grace_end_ts == at(17), "protected to the later pause end"
        assert p4(inp) == []

    def test_negative_control_ca_setpoint_reversal_inside_the_blower_pause_still_hits(self):
        """After its own pause end (14:15:30) but inside the blower's (14:17): still the occupant's setpoint."""
        reversal = Command(
            ts=at(16), target="thermostat", service="set_temperature", origin="ca", mode="cool", setpoint_f=75.0
        )
        inp = make_input(_READINGS, (reversal,), _blower_after_setpoint())
        hits = p4(inp)
        assert [h.ts for h in hits] == [at(16)]
        assert "changed your thermostat setting" in hits[0].detail

    def test_negative_control_a_replacement_the_engine_does_not_vouch_for_stays_superseded_unreplaced(self):
        inp = make_input(_READINGS, (), _blower_after_setpoint(continues=False))
        thermostat = next(e for e in inp.episodes if e.target == "thermostat")
        assert thermostat.end_reason == "superseded_unreplaced"
        assert [h.key.split(":")[1] for h in p4(inp)] == ["early_end"]

    def test_negative_control_an_automation_grace_replacing_it_stays_superseded_unreplaced(self):
        marks = events(
            (0, "override_detected", {"detected_mode": "cool"}),
            (0.5, "grace_started", {"source": "manual", "trigger": "override_confirmed", "duration_seconds": 900}),
            (
                2,
                "grace_cancelled",
                {"reason": "superseded", "trigger": "override_confirmed", "protects_override": True},
            ),
            (2, "grace_started", {"source": "automation", "trigger": "window_close", "duration_seconds": 300}),
        )
        inp = make_input(_READINGS, (), marks)
        assert next(e for e in inp.episodes if e.target == "thermostat").end_reason == "superseded_unreplaced"
        assert [h.key.split(":")[1] for h in p4(inp)] == ["early_end"]

    def test_a_shorter_replacement_is_reported_as_ending_the_setpoint_early(self):
        """Decision A2 is max(own end, replacing end): a replacing pause that ends first is an early end."""
        inp = make_input(_READINGS, (), _blower_after_setpoint(own_s=1800, fan_s=600))
        thermostat = next(e for e in inp.episodes if e.target == "thermostat")
        assert thermostat.end_reason == "grace_expired_early" and thermostat.end_ts == at(12)
        (hit,) = p4(inp)
        assert hit.key.endswith(f"early_end:{thermostat.id}")
        assert "19 min early" in hit.detail  # 18.5 min, rounded half up

    def test_a_shorter_replacement_is_reported_even_if_the_expiry_is_announced_first(self):
        """The ``grace_expired`` branch keeps the same rule, for a stream with no ``override_cleared`` before it."""
        marks = [m for m in _blower_after_setpoint(own_s=1800, fan_s=600) if m.kind != "override_cleared"]
        inp = make_input(_READINGS, (), marks)
        assert next(e for e in inp.episodes if e.target == "thermostat").end_reason == "grace_expired_early"

    def test_a_carried_setpoint_cleared_for_another_reason_keeps_that_reason(self):
        """Only the grace running out is the early end; e.g. Cancel Override inside the pause keeps its own reason."""
        marks = [
            m
            for m in _blower_after_setpoint(own_s=1800, fan_s=600)
            if m.kind not in ("override_cleared", "grace_expired")
        ] + events((5, "override_cleared", {"was_mode": "cool", "reason": "user_cancel", "continues": False}))
        inp = make_input(_READINGS, (), marks)
        assert next(e for e in inp.episodes if e.target == "thermostat").end_reason == "user_cancel"

    def test_continues_on_a_non_superseded_cancel_does_not_keep_a_thermostat_override_open(self):
        """``continues`` on a thermostat cancel that keeps the FAN run (#1205) ends the thermostat change."""
        marks = events(
            (0, "override_detected", {"detected_mode": "cool"}),
            (0.5, "grace_started", {"source": "manual", "trigger": "override_confirmed", "duration_seconds": 900}),
            (
                3,
                "grace_cancelled",
                {
                    "reason": "user_cancel_override",
                    "trigger": "override_confirmed",
                    "protects_override": True,
                    "continues": True,
                },
            ),
        )
        inp = make_input(_READINGS, (), marks)
        assert next(e for e in inp.episodes if e.target == "thermostat").end_reason == "user_cancel_override"


def _engine(fan_mode: str) -> AutomationEngine:
    hass = MagicMock()
    hass.services = MagicMock()
    hass.services.async_call = AsyncMock()
    hass.async_create_task = MagicMock(side_effect=lambda c: c.close())
    hass.states = MagicMock()
    state = MagicMock()
    state.state = "cool"
    state.attributes = {"fan_modes": ["auto", "on"]}
    hass.states.get.return_value = state
    config = {
        "comfort_heat": 70,
        "comfort_cool": 75,
        "setback_heat": 60,
        "setback_cool": 80,
        "notify_service": "notify.notify",
        "manual_grace_seconds": 900,
        CONF_FAN_MODE: fan_mode,
    }
    if fan_mode == FAN_MODE_WHOLE_HOUSE:
        config[CONF_FAN_ENTITY] = "fan.attic"
    engine = AutomationEngine(
        hass=hass,
        climate_entity="climate.thermostat",
        weather_entity="weather.forecast_home",
        door_window_sensors=["binary_sensor.front_door"],
        notify_service="notify.notify",
        config=config,
    )
    engine.seen = []
    engine._emit_event_callback = lambda t, d: engine.seen.append((t, dict(d)))
    return engine


def _protected_setpoint_grace(engine: AutomationEngine) -> None:
    engine._manual_override_active = True
    engine._manual_override_mode = "cool"
    assert engine._start_grace_period_action("manual", trigger="override_confirmed")
    engine._grace_active = True
    engine._grace_protects_override = True


def _superseded(engine: AutomationEngine) -> list[dict]:
    return [d for t, d in engine.seen if t == "grace_cancelled" and d["reason"] == "superseded"]


class TestEngineVouchesForTheSetpoint:
    def test_fan_override_grace_replacing_a_protected_setpoint_grace_says_it_continues(self):
        engine = _engine(FAN_MODE_HVAC)
        _protected_setpoint_grace(engine)
        assert engine._start_grace_period_action("manual", trigger="fan_manual_override")
        assert [d["continues"] for d in _superseded(engine)] == [True]

    def test_no_continues_once_rule2_has_replaced_the_thermostat_change(self):
        """A whole-house-fan press clears the thermostat change first (rule 2): nothing to keep."""
        engine = _engine(FAN_MODE_WHOLE_HOUSE)
        _protected_setpoint_grace(engine)
        engine._manual_override_active = False
        assert engine._start_grace_period_action("manual", trigger="fan_manual_override")
        assert [d["continues"] for d in _superseded(engine)] == [False]

    def test_no_continues_for_a_replacement_that_is_not_a_fan_override_grace(self):
        engine = _engine(FAN_MODE_HVAC)
        _protected_setpoint_grace(engine)
        assert engine._start_grace_period_action("manual", trigger="fan_off")
        assert [d["continues"] for d in _superseded(engine)] == [False]


class TestJournalReadsTheEnginesOwnEventOrder:
    """The real engine's event stream, fed to the journal (guards against a test-only event order, Issue #1204)."""

    def _engine_stream(self, *, fan_s: int):
        """Setpoint change (own grace 900 s at 14:00:30), blower press at 14:02 (fan grace ``fan_s``), its expiry."""
        engine = _engine(FAN_MODE_HVAC)
        _protected_setpoint_grace(engine)
        phase1 = list(engine.seen)
        engine._fan_override_active = True
        engine._fan_override_is_hvac_fan = True
        engine._fan_override_time = at(2).isoformat()
        assert engine._start_grace_period_action("manual", trigger="fan_manual_override", duration_override=fan_s)
        phase2 = engine.seen[len(phase1) :]
        engine._any_monitored_sensor_settled_open = lambda: False
        engine._on_grace_expired("manual", fan_s, False)
        phase3 = engine.seen[len(phase1) + len(phase2) :]
        marks = events((0, "override_detected", {"detected_mode": "cool"}))
        for minutes, phase in ((0.5, phase1), (2, phase2), (2 + fan_s / 60, phase3)):
            marks += [mark_from_event(at(minutes), t, d) for t, d in phase]
        return [t for t, _ in phase3], make_input(_READINGS, (), marks)

    def test_the_engine_clears_the_override_before_announcing_the_expiry(self):
        kinds, _ = self._engine_stream(fan_s=600)
        assert [k for k in kinds if k in ("override_cleared", "grace_expired")] == ["override_cleared", "grace_expired"]

    def test_a_shorter_replacement_in_the_engines_order_is_an_early_end(self):
        _, inp = self._engine_stream(fan_s=600)  # fan grace ends 14:12, the setpoint's own at 14:15:30
        thermostat = next(e for e in inp.episodes if e.target == "thermostat")
        assert thermostat.end_reason == "grace_expired_early" and thermostat.end_ts == at(12)
        assert [h.key.split(":")[1] for h in p4(inp)] == ["early_end"]

    def test_a_longer_replacement_in_the_engines_order_is_no_miss(self):
        _, inp = self._engine_stream(fan_s=1200)  # fan grace ends 14:22, after the setpoint's own
        thermostat = next(e for e in inp.episodes if e.target == "thermostat")
        assert thermostat.end_reason == "grace_expired" and thermostat.end_ts == at(22)
        assert p4(inp) == []


class TestRule2StandDownEndsTheFanOverride:
    """Decision A3: the stand-down clears the older fan override once its stop has gone out (all 4 callers)."""

    def _whf_fan_override(self, *, with_grace: bool) -> AutomationEngine:
        engine = _engine(FAN_MODE_WHOLE_HOUSE)
        engine._fan_override_active = True
        engine._fan_override_time = T0.isoformat()
        engine._get_fan_physical_state_callback = lambda: True
        if with_grace:
            assert engine._start_grace_period_action("manual", trigger="fan_manual_override")
            engine._grace_active = True
            engine._grace_protects_override = True
        return engine

    @pytest.mark.parametrize("with_grace", [False, True], ids=["grace0", "grace900"])
    def test_stand_down_before_the_cool_is_pending_ends_the_fan_override_and_keeps_the_grace(self, with_grace):
        """Nothing pending or confirmed yet: the clear does not depend on the caller's ordering."""
        engine = self._whf_fan_override(with_grace=with_grace)
        asyncio.run(engine._stand_down_whf_for_override_conflict(mode="cool", indoor_temp=72.0, event_source="t"))
        assert engine._fan_override_active is False
        cleared = [d for t, d in engine.seen if t == "fan_override_cleared"]
        assert [d["reason"] for d in cleared] == ["whf_safety_stop"]
        assert engine._grace_active is with_grace, "keep_grace: the stand-down never cancels a grace"
        assert not [d for t, d in engine.seen if t == "grace_cancelled"]

    def test_stand_down_from_a_confirmed_override_tick_ends_the_fan_override(self):
        engine = self._whf_fan_override(with_grace=True)
        engine._manual_override_active = True
        engine._manual_override_mode = "cool"
        asyncio.run(engine._stand_down_whf_for_override_conflict(mode="cool", indoor_temp=72.0, event_source=None))
        assert engine._fan_override_active is False and engine._grace_active is True

    @pytest.mark.parametrize("result", [FanCommandResult.RATE_LIMITED_NEW, FanCommandResult.RATE_LIMITED_DUP])
    def test_a_stop_that_did_not_go_out_keeps_the_fan_override(self, result):
        engine = self._whf_fan_override(with_grace=False)
        engine._deactivate_fan = AsyncMock(return_value=result)
        asyncio.run(engine._stand_down_whf_for_override_conflict(mode="cool", indoor_temp=72.0, event_source="t"))
        assert engine._fan_override_active is True
        assert not [t for t, _ in engine.seen if t == "fan_override_cleared"]

    def test_observe_only_keeps_the_fan_override_because_no_stop_was_sent(self):
        """Dry run: ``_deactivate_fan()`` reports EXECUTED without sending; the occupant's fan is still running."""
        engine = self._whf_fan_override(with_grace=True)
        engine.dry_run = True
        asyncio.run(engine._stand_down_whf_for_override_conflict(mode="cool", indoor_temp=72.0, event_source="t"))
        assert not [c for c in engine.hass.services.async_call.call_args_list if c.args[:1] == ("fan",)]
        assert engine._fan_override_active is True
        assert not [t for t, _ in engine.seen if t == "fan_override_cleared"]

    @pytest.mark.parametrize("fan_mode", [FAN_MODE_HVAC, "both"])
    def test_a_newer_blower_override_is_never_ended_by_a_stand_down(self, fan_mode):
        """Fan-identity guard: only a whole-house-fan override is ended; the thermostat's blower never conflicts."""
        engine = _engine(fan_mode)
        engine._fan_override_active = True
        engine._fan_override_is_hvac_fan = True
        engine._fan_override_time = T0.isoformat()
        engine._deactivate_fan = AsyncMock(return_value=FanCommandResult.EXECUTED)
        asyncio.run(engine._stand_down_whf_for_override_conflict(mode="cool", indoor_temp=72.0, event_source=None))
        assert engine._fan_override_active is True
        assert not [t for t, _ in engine.seen if t == "fan_override_cleared"]

    def test_a_whf_override_under_both_is_ended(self):
        """Control for the guard: under ``both`` a whole-house-fan press is still the one the stand-down ends."""
        engine = _engine("both")
        engine.config[CONF_FAN_ENTITY] = "fan.attic"
        engine._fan_override_active = True
        engine._fan_override_is_hvac_fan = False
        engine._fan_override_time = T0.isoformat()
        engine._deactivate_fan = AsyncMock(return_value=FanCommandResult.EXECUTED)
        asyncio.run(engine._stand_down_whf_for_override_conflict(mode="cool", indoor_temp=72.0, event_source=None))
        assert engine._fan_override_active is False


class TestRule2SafetyStopInTheStartupWindowEndsTheFanOverride:
    """Mirror of decision A3 on the WHF safety enforcer's rule-2 (mutex) stop (``_after_whf_safety_stop``).

    Occupant story: minutes after an HA restart, the fan override is restored (or the person presses the fan), then
    the person sets Heat. The 300 s start-up coalescing window suppresses override detection, so their Heat is neither
    pending nor confirmed. The enforcer stops the fan (rule 2), and the fan card must not keep saying
    "off (manual override)" until the fan's grace ends (hours with an RF timer).
    """

    def _engine_after_stop(self, **flags) -> AutomationEngine:
        engine = _engine(FAN_MODE_WHOLE_HOUSE)
        engine._fan_override_active = True
        engine._fan_override_time = T0.isoformat()
        assert engine._start_grace_period_action("manual", trigger="fan_manual_override")
        engine._grace_active = True
        engine._grace_protects_override = True
        for k, v in flags.items():
            setattr(engine, k, v)
        engine._spawn_whf_task = lambda c: c.close()
        engine._after_whf_safety_stop(FanCommandResult.EXECUTED, sealed=False, reason="r", trigger="thermostat_mode")
        return engine

    def test_nothing_protected_ends_the_fan_override_and_its_grace(self):
        engine = self._engine_after_stop()
        assert engine._fan_override_active is False
        assert [d["reason"] for t, d in engine.seen if t == "fan_override_cleared"] == ["whf_safety_stop"]
        assert engine._grace_active is False, "a grace protecting nothing would be reaped as a Stuck grace"
        assert [d["reason"] for t, d in engine.seen if t == "grace_cancelled"] == ["whf_safety_stop"]

    @pytest.mark.parametrize("protected", ["_override_confirm_pending", "_manual_override_active"])
    def test_a_pending_or_confirmed_hvac_change_keeps_its_grace(self, protected):
        engine = self._engine_after_stop(**{protected: True})
        assert engine._fan_override_active is False
        assert engine._grace_active is True
        assert not [t for t, _ in engine.seen if t == "grace_cancelled"]

    def test_the_sealed_house_stop_is_unchanged(self):
        """Rule 1 keeps its own semantics (the fan-owned grace is cancelled with the override)."""
        engine = _engine(FAN_MODE_WHOLE_HOUSE)
        engine._fan_override_active = True
        engine._fan_override_time = T0.isoformat()
        assert engine._start_grace_period_action("manual", trigger="fan_manual_override")
        engine._grace_active = True
        engine._grace_protects_override = True
        engine._spawn_whf_task = lambda c: c.close()
        engine._after_whf_safety_stop(FanCommandResult.EXECUTED, sealed=True, reason="r", trigger="tick")
        assert engine._fan_override_active is False and engine._grace_active is False
        assert [d["reason"] for t, d in engine.seen if t == "grace_cancelled"] == ["whf_safety_stop"]


class TestKeptGraceThatProtectsNothingEndsOnPurpose:
    """Edge of decision A3: the kept grace outlives a pending change that self-resolved."""

    def _kept_grace(self) -> AutomationEngine:
        engine = _engine(FAN_MODE_WHOLE_HOUSE)
        assert engine._start_grace_period_action("manual", trigger="fan_manual_override")
        engine._grace_active = True
        engine._grace_protects_override = True
        return engine

    def test_nothing_left_to_protect_ends_the_grace_with_its_reason(self):
        engine = self._kept_grace()
        engine._end_grace_left_protecting_nothing("override_self_resolved")
        assert engine._grace_active is False
        assert [d["reason"] for t, d in engine.seen if t == "grace_cancelled"] == ["override_self_resolved"]

    @pytest.mark.parametrize("still", ["_override_confirm_pending", "_manual_override_active", "_fan_override_active"])
    def test_a_grace_that_still_protects_something_is_kept(self, still):
        engine = self._kept_grace()
        setattr(engine, still, True)
        engine._end_grace_left_protecting_nothing("override_self_resolved")
        assert engine._grace_active is True
        assert not [t for t, _ in engine.seen if t == "grace_cancelled"]
