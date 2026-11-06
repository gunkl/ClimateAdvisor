"""Read-back "did the thermostat take the mode?" check and honest write outcomes (Issue #1049, #1048 changes 2 + 5).

Real ``AutomationEngine`` throughout. The thermostat in the engine-level tests is a static mock
whose state we flip by hand to model a device that ignores / later obeys a mode command.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.climate_advisor import ai_skills_context as _ctx
from custom_components.climate_advisor import automation as automation_module
from custom_components.climate_advisor.automation import (
    AutomationEngine,
    ComfortBand,
    HvacCommandResult,
    HvacWriteRecord,
    hvac_outcome_label,
)
from custom_components.climate_advisor.setpoint_verify_decision import ModeAlignmentOutcome, decide_mode_alignment

NOW = datetime(2026, 1, 12, 7, 0, 0, tzinfo=UTC)


def _kw(**over):
    base = dict(
        current_write_seq=3,
        check_write_seq=3,
        requested_mode="heat",
        live_state="off",
        manual_override_active=False,
        override_confirm_pending=False,
        paused_by_door=False,
        whf_owns_hvac=False,
        dry_run=False,
        command_in_progress=False,
    )
    base.update(over)
    return base


class TestDecideModeAlignment:
    def test_misaligned_and_aligned(self):
        assert decide_mode_alignment(**_kw()) is ModeAlignmentOutcome.MISALIGNED
        assert decide_mode_alignment(**_kw(live_state="heat")) is ModeAlignmentOutcome.ALIGNED

    def test_off_direction_requested_off_thermostat_still_on(self):
        assert decide_mode_alignment(**_kw(requested_mode="off", live_state="heat")) is ModeAlignmentOutcome.MISALIGNED
        assert decide_mode_alignment(**_kw(requested_mode="off", live_state="off")) is ModeAlignmentOutcome.ALIGNED

    def test_stale_and_nothing_requested(self):
        assert decide_mode_alignment(**_kw(check_write_seq=2)) is ModeAlignmentOutcome.STALE
        assert decide_mode_alignment(**_kw(requested_mode=None)) is ModeAlignmentOutcome.NOT_APPLICABLE

    @pytest.mark.parametrize(
        ("flag", "expected"),
        [
            ("dry_run", ModeAlignmentOutcome.EXCLUDED_DRY_RUN),
            ("manual_override_active", ModeAlignmentOutcome.EXCLUDED_OVERRIDE),
            ("override_confirm_pending", ModeAlignmentOutcome.EXCLUDED_OVERRIDE),
            ("whf_owns_hvac", ModeAlignmentOutcome.EXCLUDED_WHF),
            ("paused_by_door", ModeAlignmentOutcome.EXCLUDED_PAUSED),
            ("command_in_progress", ModeAlignmentOutcome.EXCLUDED_BUSY),
        ],
    )
    def test_exclusions(self, flag, expected):
        assert decide_mode_alignment(**_kw(**{flag: True})) is expected

    def test_door_pause_excludes_heat_cool_requests_but_not_an_off_request(self):
        assert (
            decide_mode_alignment(**_kw(paused_by_door=True, requested_mode="off", live_state="heat"))
            is ModeAlignmentOutcome.MISALIGNED
        )

    @pytest.mark.parametrize("live", [None, "", "unavailable", "unknown", 3])
    def test_unreadable_thermostat_cannot_be_judged(self, live):
        assert decide_mode_alignment(**_kw(live_state=live)) is ModeAlignmentOutcome.UNREADABLE


def _engine(state: str = "off", *, temperature=None) -> AutomationEngine:
    hass = MagicMock()
    hass.services.async_call = AsyncMock()
    hass.async_create_task = MagicMock(side_effect=lambda coro: coro.close())
    st = MagicMock()
    st.state = state
    attrs = {"hvac_modes": ["off", "heat", "cool"], "supported_features": 1}
    if temperature is not None:
        attrs["temperature"] = temperature  # Sensi reports no temperature while Off
    st.attributes = attrs
    hass.states = MagicMock()
    hass.states.get.return_value = st
    config = {
        "temp_unit": "fahrenheit",
        "comfort_heat": 70,
        "comfort_cool": 75,
        "setback_heat": 60,
        "setback_cool": 80,
        "notify_service": "notify.notify",
    }
    eng = AutomationEngine(
        hass=hass,
        climate_entity="climate.thermostat",
        weather_entity="weather.forecast_home",
        door_window_sensors=[],
        notify_service="notify.notify",
        config=config,
    )
    eng._events = []
    eng._emit_event_callback = lambda n, d: eng._events.append((n, d))
    return eng


class _Timers:
    """Capture async_call_later registrations so a test can fire them by delay."""

    def __init__(self):
        self.items: list[tuple[float, object]] = []
        self.cancelled = 0

    def __call__(self, hass, delay, cb):
        self.items.append((delay, cb))
        outer = self

        def cancel():
            outer.cancelled += 1

        return cancel

    def fire(self, engine, delay):
        cb = [c for d, c in self.items if d == delay][-1]
        coros: list = []
        engine.hass.async_create_task = MagicMock(side_effect=lambda c: coros.append(c))
        cb(None)
        for c in coros:
            asyncio.run(c)


def _patched(timers):
    return (
        patch.object(automation_module, "async_call_later", side_effect=timers),
        patch.object(automation_module, "callback", side_effect=lambda fn: fn),
        patch.object(automation_module.dt_util, "now", return_value=NOW),
    )


def _names(engine):
    return [n for n, _ in engine._events]


class TestReadBackEpisode:
    def test_thermostat_ignoring_a_heat_request_is_announced_once_then_recovers(self):
        engine = _engine("off")
        # Mode commands "succeed" but the device never switches: state stays off.
        timers = _Timers()
        p1, p2, p3 = _patched(timers)
        with p1, p2, p3:
            asyncio.run(engine._set_temperature(70.0, reason="t", mode="heat"))
            assert {d for d, _ in timers.items} == {10, 130}
            timers.fire(engine, 10)  # early check: starts the mismatch clock, never announces
            assert "hvac_mode_not_applied" not in _names(engine)
            assert engine._hvac_write_record.mismatch_since is not None
            timers.fire(engine, 130)
            timers.fire(engine, 130)  # second miss in the same episode: silent
            assert _names(engine).count("hvac_mode_not_applied") == 1
            ev = next(d for n, d in engine._events if n == "hvac_mode_not_applied")
            assert (ev["kind"], ev["requested_mode"], ev["actual_mode"]) == ("not_obeyed", "heat", "off")
            # The thermostat finally obeys.
            engine.hass.states.get.return_value.state = "heat"
            timers.fire(engine, 130)
        assert _names(engine).count("hvac_mode_applied_recovered") == 1
        assert engine._hvac_write_record.episode_reason is None
        assert engine._hvac_write_record.mismatch_since is None

    def test_early_check_runs_before_the_no_reading_exit(self):
        """Sensi reports temperature=None while Off: the old 10 s closure returned at NO_READING."""
        engine = _engine("off", temperature=None)
        timers = _Timers()
        p1, p2, p3 = _patched(timers)
        with p1, p2, p3:
            asyncio.run(engine._set_temperature(70.0, reason="t", mode="heat"))
            timers.fire(engine, 10)
        assert engine._hvac_write_record.mismatch_since is not None

    def test_off_direction_set_hvac_mode_off_ignored_by_thermostat(self):
        engine = _engine("heat")
        timers = _Timers()
        p1, p2, p3 = _patched(timers)
        with p1, p2, p3:
            asyncio.run(engine._set_hvac_mode("off", reason="door/window pause"))
            timers.fire(engine, 130)
        ev = next(d for n, d in engine._events if n == "hvac_mode_not_applied")
        assert (ev["requested_mode"], ev["actual_mode"]) == ("off", "heat")

    def test_newer_write_makes_the_old_check_stale_and_replaces_the_timer(self):
        engine = _engine("off")
        timers = _Timers()
        p1, p2, p3 = _patched(timers)
        with p1, p2, p3:
            asyncio.run(engine._set_temperature(70.0, reason="a", mode="heat"))
            first = [c for d, c in timers.items if d == 130][-1]
            asyncio.run(engine._set_temperature(71.0, reason="b", mode="heat"))
            assert timers.cancelled >= 1, "the older recheck must be cancelled, not left running"
            first(None)  # stale seq -> nothing
        assert "hvac_mode_not_applied" not in _names(engine)

    @pytest.mark.parametrize(
        "setup",
        [
            lambda e: setattr(e, "_manual_override_active", True),
            lambda e: setattr(e, "_override_confirm_pending", True),
            lambda e: setattr(e, "_whf_owns_hvac", lambda: True),
        ],
    )
    def test_user_or_fan_owned_thermostat_is_never_reported(self, setup):
        engine = _engine("off")
        timers = _Timers()
        p1, p2, p3 = _patched(timers)
        with p1, p2, p3:
            asyncio.run(engine._set_temperature(70.0, reason="t", mode="heat"))
            setup(engine)
            timers.fire(engine, 130)
        assert "hvac_mode_not_applied" not in _names(engine)
        assert engine._hvac_write_record.apply_skip_reason.startswith("excluded_")

    def test_door_pause_excludes_a_heat_request_and_unavailable_thermostat_is_not_judged(self):
        engine = _engine("off")
        timers = _Timers()
        p1, p2, p3 = _patched(timers)
        with p1, p2, p3:
            asyncio.run(engine._set_temperature(70.0, reason="t", mode="heat"))
            engine._paused_by_door = True
            timers.fire(engine, 130)
            engine._paused_by_door = False
            engine.hass.states.get.return_value.state = "unavailable"
            timers.fire(engine, 130)
        assert "hvac_mode_not_applied" not in _names(engine)
        assert engine._hvac_write_record.apply_skip_reason == "unreadable"

    def test_dry_run_schedules_nothing(self):
        engine = _engine("off")
        engine.dry_run = True
        timers = _Timers()
        p1, p2, p3 = _patched(timers)
        with p1, p2, p3:
            asyncio.run(engine._set_temperature(70.0, reason="t", mode="heat"))
            asyncio.run(engine._set_hvac_mode("heat", reason="t"))
        assert timers.items == []

    def test_cleanup_cancels_the_recheck_slot(self):
        engine = _engine("off")
        timers = _Timers()
        p1, p2, p3 = _patched(timers)
        with p1, p2, p3:
            asyncio.run(engine._set_temperature(70.0, reason="t", mode="heat"))
            assert engine._mode_align_recheck_cancel is not None
            before = timers.cancelled
            engine.cleanup()
        assert timers.cancelled > before
        assert engine._mode_align_recheck_cancel is None

    def test_write_record_is_frozen_and_holds_only_new_facts(self):
        rec = HvacWriteRecord()
        with pytest.raises(AttributeError):
            rec.result = HvacCommandResult.EXECUTED  # type: ignore[misc]
        assert not hasattr(rec, "commanded_mode")


class TestHonestWriteOutcomes:
    def test_outcome_labels(self):
        assert hvac_outcome_label(HvacCommandResult.EXECUTED) == "sent"
        assert hvac_outcome_label(HvacCommandResult.UNCONFIRMED) == "sent"
        assert hvac_outcome_label(HvacCommandResult.WHF_BLOCKED) == "blocked"
        assert hvac_outcome_label(HvacCommandResult.DRY_RUN) == "dry_run"
        assert hvac_outcome_label(HvacCommandResult.FAILED) == "failed"
        assert hvac_outcome_label(None) == "not_sent"

    def _band(self, engine, active="ceiling"):
        band = ComfortBand(floor=70.0, ceiling=75.0, active=active, reason="test band")
        with patch.object(automation_module.dt_util, "now", return_value=NOW):
            return asyncio.run(engine._apply_comfort_band(band, reason="t"))

    def _applied(self, engine):
        return [d for n, d in engine._events if n == "comfort_band_applied"]

    def test_sent(self):
        engine = _engine("cool")
        assert self._band(engine) in (HvacCommandResult.EXECUTED,)
        assert self._applied(engine)[0]["outcome"] == "sent"

    def test_whf_blocked_is_not_reported_as_applied(self):
        engine = _engine("cool")
        engine._whf_owns_hvac = lambda: True
        self._band(engine)
        assert self._applied(engine)[0]["outcome"] == "blocked"

    def test_dry_run(self):
        engine = _engine("cool")
        engine.dry_run = True
        self._band(engine)
        assert self._applied(engine)[0]["outcome"] == "dry_run"

    def test_failed_then_sent_are_announced_separately(self):
        engine = _engine("cool")
        engine.hass.services.async_call.side_effect = RuntimeError("boom")
        self._band(engine)
        engine.hass.services.async_call.side_effect = None
        self._band(engine)
        assert [d["outcome"] for d in self._applied(engine)] == ["failed", "sent"]

    def test_reason_names_the_effective_edge_when_the_family_switch_overrides_the_band(self):
        engine = _engine("cool")
        engine._resolve_comfort_family_via_fsm = lambda day_mode, **kw: "heat"
        self._band(engine, active="ceiling")
        payload = self._applied(engine)[0]
        assert payload["active"] == "floor"
        assert "floor edge used" in payload["reason"]
        assert payload["reason"].startswith("test band")

    def test_no_capable_mode_means_nothing_sent_and_no_applied_claim(self):
        engine = _engine("cool")
        engine.hass.states.get.return_value.attributes = {"hvac_modes": [], "supported_features": 0}
        assert self._band(engine) is None
        assert self._applied(engine) == []

    def test_pre_cool_result_and_event_say_not_applied_when_nothing_was_sent(self):
        engine = _engine("cool")
        engine.hass.states.get.return_value.attributes = {"hvac_modes": [], "supported_features": 0}
        from custom_components.climate_advisor.classifier import DayClassification

        c = object.__new__(DayClassification)
        for k, v in dict(
            day_type="warm",
            hvac_mode="cool",
            trend_direction="warming",
            trend_magnitude=3.0,
            today_high=88.0,
            today_low=65.0,
            tomorrow_high=95.0,
            tomorrow_low=70.0,
            pre_condition=False,
            pre_condition_target=None,
            windows_recommended=False,
            window_open_time=None,
            window_close_time=None,
            setback_modifier=-3.0,
        ).items():
            setattr(c, k, v)
        engine._current_classification = c
        # Issue #1371: pre-cool runs only in the sleep window, so this fires at 02:30 (NOW is 07:00, awake).
        _night = NOW.replace(hour=2, minute=30)
        with patch.object(automation_module.dt_util, "now", return_value=_night):
            result = asyncio.run(engine.handle_pre_cool(indoor_temp=76.5, nat_vent_just_closed=False))
        assert result.startswith("not applied"), result
        ev = [d for n, d in engine._events if n == "pre_cool_applied"]
        assert ev and ev[0]["outcome"] == "not_sent"

    def test_tou_av_precondition_carries_the_outcome(self):
        engine = _engine("cool")
        engine.dry_run = True
        engine.set_occupancy_mode("away")
        from custom_components.climate_advisor.classifier import DayClassification

        c = object.__new__(DayClassification)
        c.day_type, c.hvac_mode = "warm", "cool"
        with patch.object(automation_module.dt_util, "now", return_value=NOW):
            asyncio.run(engine.apply_tou_away_vacation_precondition(c, 72.0, "sched1", "cool"))
        ev = [d for n, d in engine._events if n == "tou_av_precondition_applied"]
        assert ev and ev[0]["outcome"] == "dry_run"


class TestRenderers:
    def test_not_sent_wording_and_back_compat(self):
        label, _ = _ctx.EVENT_RENDERERS["comfort_band_applied"]({"mode": "heat", "outcome": "failed"}, "fahrenheit")
        assert "NOT applied" in label and "command failed" in label
        label, _ = _ctx.EVENT_RENDERERS["comfort_band_applied"]({"mode": "heat", "outcome": "sent"}, "fahrenheit")
        assert label.startswith("Comfort band applied")
        label, _ = _ctx.EVENT_RENDERERS["comfort_band_applied"]({"mode": "heat"}, "fahrenheit")  # historical event
        assert label.startswith("Comfort band applied")
        label, _ = _ctx.EVENT_RENDERERS["pre_cool_applied"]({"outcome": "blocked", "target": 70}, "fahrenheit")
        assert label.startswith("Pre-cool NOT applied")
        label, _ = _ctx.EVENT_RENDERERS["tou_precondition_applied"](
            {"mode": "cool", "outcome": "dry_run"}, "fahrenheit"
        )
        assert "NOT applied" in label

    def test_new_event_renderers_and_registrations(self):
        label, settings = _ctx.EVENT_RENDERERS["hvac_mode_not_applied"](
            {"kind": "not_obeyed", "requested_mode": "heat", "actual_mode": "off"}, "fahrenheit"
        )
        assert "did not switch to heat" in label and "off" in settings
        label, _ = _ctx.EVENT_RENDERERS["hvac_mode_not_applied"]({"kind": "rejected", "requested_mode": "auto"}, "f")
        assert "rejected" in label
        label, settings = _ctx.EVENT_RENDERERS["hvac_mode_applied_recovered"](
            {"requested_mode": "heat", "minutes_down": 7}, "fahrenheit"
        )
        assert "now in heat" in label and "7" in settings
        label, _ = _ctx.EVENT_RENDERERS["hvac_command_recovered"]({"minutes_down": 3}, "fahrenheit")
        assert "accepting commands" in label
        for ev in ("hvac_mode_not_applied", "hvac_mode_applied_recovered", "hvac_command_recovered"):
            assert ev in _ctx._NO_DEDUP
