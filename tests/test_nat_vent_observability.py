"""Observability tests for nat-vent / grace decisions (Issue #1095).

Occupant story: the whole-house fan did not run while outdoor air was cooler and a window
was open, because a fan-off grace period was deliberately holding nat-vent back — but
nothing in the log said so. These tests pin the fix at the points the occupant/developer
actually reads: the log line for a skipped evaluation, the gate's rejection reasons, the
grace-start log, and the cancel endpoint's wording for a grace that has no override behind it.

Every test drives the real engine/gate/view — none re-implements the logic under test.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.climate_advisor.automation import AutomationEngine
from custom_components.climate_advisor.nat_vent_gate import (
    FAN_MODE_WHOLE_HOUSE,
    NatVentGateInputs,
    NatVentSoftStartGateInputs,
    decide_nat_vent_gate,
    decide_nat_vent_soft_start_gate,
    explain_nat_vent_gate,
    explain_nat_vent_soft_start_gate,
)

_AUTOMATION_LOGGER = "custom_components.climate_advisor.automation"
_DT_PATH = "custom_components.climate_advisor.automation.dt_util"
_NOW = datetime(2026, 10, 11, 21, 15, 0)
_GRACE_END = "2026-10-11T21:45:34"


# ---------------------------------------------------------------------------
# Gate: rejection reasons
# ---------------------------------------------------------------------------


def _gate_inputs(**overrides) -> NatVentGateInputs:
    base = {
        "outdoor": 60.0,
        "indoor": 72.0,
        "comfort_heat_raw": 68.0,
        "sleep_heat": 66.0,
        "in_sleep_window": False,
        "comfort_cool": 74.0,
        "nat_vent_delta": 3.0,
        "hysteresis": 1.0,
        "fan_mode": FAN_MODE_WHOLE_HOUSE,
        "aggressive_savings": False,
    }
    base.update(overrides)
    return NatVentGateInputs(**base)


class TestExplainNatVentGate:
    def test_passing_inputs_have_no_rejections(self):
        inputs = _gate_inputs()
        assert explain_nat_vent_gate(inputs) == []
        assert decide_nat_vent_gate(inputs) is True

    @pytest.mark.parametrize(
        ("overrides", "expected"),
        [
            ({"outdoor": None}, ["temperature_unavailable"]),
            ({"indoor": None}, ["temperature_unavailable"]),
            # outdoor not below indoor - hysteresis (71.5 !< 72 - 1)
            ({"outdoor": 71.5}, ["outdoor_not_cooler_than_indoor"]),
            # indoor at the comfort floor (strict '>')
            ({"indoor": 68.0, "outdoor": 60.0}, ["indoor_at_or_below_comfort_floor"]),
            # outdoor at the threshold comfort_cool + delta = 77 (strict '<'); indoor high enough to pass the delta test
            ({"indoor": 90.0, "outdoor": 77.0}, ["outdoor_above_threshold"]),
            # hvac_fan + aggressive savings: ceiling = comfort_cool + 2 = 76; indoor 80 breaches it
            (
                {"fan_mode": "hvac_fan", "aggressive_savings": True, "indoor": 80.0, "outdoor": 60.0},
                ["indoor_above_ceiling"],
            ),
            # several at once are all reported, in a stable order
            (
                {"indoor": 67.0, "outdoor": 80.0},
                ["outdoor_not_cooler_than_indoor", "indoor_at_or_below_comfort_floor", "outdoor_above_threshold"],
            ),
        ],
    )
    def test_each_failed_condition_is_named(self, overrides, expected):
        inputs = _gate_inputs(**overrides)
        assert explain_nat_vent_gate(inputs) == expected
        # The boolean decision and its explanation come from one implementation.
        assert decide_nat_vent_gate(inputs) is False

    def test_sleep_window_uses_sleep_floor(self):
        # indoor 67 is below the daytime floor (68) but above the sleep floor (66)
        inputs = _gate_inputs(indoor=67.0, outdoor=60.0, in_sleep_window=True)
        assert explain_nat_vent_gate(inputs) == []
        assert explain_nat_vent_gate(_gate_inputs(indoor=67.0, outdoor=60.0)) == ["indoor_at_or_below_comfort_floor"]


def _soft_inputs(**overrides) -> NatVentSoftStartGateInputs:
    base = {
        "outdoor": 74.0,
        "indoor": 76.0,
        "comfort_heat": 68.0,
        "comfort_cool": 74.0,
        "fan_mode": FAN_MODE_WHOLE_HOUSE,
        "outdoor_today_peak": 85.0,
        "outdoor_sample_count": 10,
        "peak_decline_margin": 2.0,
        "full_gate_active": False,
    }
    base.update(overrides)
    return NatVentSoftStartGateInputs(**base)


class TestExplainSoftStartGate:
    def test_passing_inputs_have_no_rejections(self):
        inputs = _soft_inputs()
        assert explain_nat_vent_soft_start_gate(inputs) == []
        assert decide_nat_vent_soft_start_gate(inputs) is True

    @pytest.mark.parametrize(
        ("overrides", "expected"),
        [
            ({"outdoor": None}, ["temperature_unavailable"]),
            ({"full_gate_active": True}, ["full_gate_already_applies"]),
            ({"fan_mode": "hvac_fan"}, ["fan_mode_not_whole_house"]),
            ({"outdoor_sample_count": 1}, ["insufficient_peak_samples"]),
            ({"outdoor_today_peak": None}, ["insufficient_peak_samples"]),
            ({"outdoor_today_peak": 75.0}, ["outdoor_not_past_peak"]),
            (
                {"indoor": 68.0, "outdoor": 60.0},
                ["indoor_at_or_below_comfort_floor", "indoor_at_or_below_comfort_ceiling"],
            ),
            ({"outdoor": 77.0, "outdoor_today_peak": 90.0}, ["outdoor_warmer_than_indoor"]),
        ],
    )
    def test_each_failed_condition_is_named(self, overrides, expected):
        inputs = _soft_inputs(**overrides)
        assert explain_nat_vent_soft_start_gate(inputs) == expected
        assert decide_nat_vent_soft_start_gate(inputs) is False


# ---------------------------------------------------------------------------
# Engine: skipped / not-engaged evaluations are logged with their reason
# ---------------------------------------------------------------------------


def _make_engine(indoor_f: float = 67.5) -> AutomationEngine:
    hass = MagicMock()
    hass.services = MagicMock()
    hass.services.async_call = AsyncMock()

    def _consume_coroutine(coro):
        coro.close()

    hass.async_create_task = MagicMock(side_effect=_consume_coroutine)
    climate_state = MagicMock()
    climate_state.state = "off"
    climate_state.attributes = {"current_temperature": indoor_f, "fan_modes": ["auto", "on"], "hvac_action": "idle"}
    hass.states = MagicMock()
    hass.states.get = MagicMock(return_value=climate_state)
    config = {
        "comfort_heat": 68.0,
        "comfort_cool": 74.0,
        "setback_heat": 60,
        "setback_cool": 80,
        "natural_vent_delta": 3.0,
        "fan_mode": FAN_MODE_WHOLE_HOUSE,
        "notify_service": "notify.notify",
    }
    return AutomationEngine(
        hass=hass,
        climate_entity="climate.thermostat",
        weather_entity="weather.forecast_home",
        door_window_sensors=["binary_sensor.front_door"],
        notify_service="notify.notify",
        config=config,
    )


def _arm_fan_off_grace_while_paused(engine: AutomationEngine) -> None:
    """The exact state from the 2026-10-11 incident: window open, HVAC was already off when
    the pause began, and a fan-off manual grace is running with no override flag behind it."""
    engine._paused_by_door = True
    engine._paused_with_hvac_already_off = True
    engine._sensor_check_callback = lambda: True
    engine._last_outdoor_temp = 59.4
    engine._grace_active = True
    engine._last_resume_source = "manual"
    engine._last_grace_trigger = "fan_off"
    engine._grace_end_time = _GRACE_END


def _run_check(engine: AutomationEngine) -> None:
    with (
        patch(f"{_DT_PATH}.now", return_value=_NOW),
        patch(f"{_DT_PATH}.parse_datetime", side_effect=lambda s: datetime.fromisoformat(s)),
        patch(f"{_DT_PATH}.as_local", side_effect=lambda x: x),
    ):
        asyncio.run(engine.check_natural_vent_conditions())


class TestSkippedEvaluationIsLogged:
    def test_grace_blocked_skip_names_reason_trigger_and_end_time(self, caplog):
        engine = _make_engine()
        _arm_fan_off_grace_while_paused(engine)
        caplog.set_level(logging.DEBUG, logger=_AUTOMATION_LOGGER)

        _run_check(engine)

        msgs = [r.getMessage() for r in caplog.records if "Nat-vent evaluation" in r.getMessage()]
        assert len(msgs) == 1, msgs
        msg = msgs[0]
        assert "Nat-vent evaluation skipped" in msg
        assert "reason=grace_active" in msg
        assert "trigger=fan_off" in msg
        assert "source=manual" in msg
        assert "ends=21:45" in msg
        assert "indoor=67.5" in msg
        assert "outdoor=59.4" in msg
        # No decision side effects: nat-vent stayed off and nothing was commanded.
        assert engine._natural_vent_active is False
        engine.hass.services.async_call.assert_not_called()

    def test_first_occurrence_is_info_then_repeat_is_debug(self, caplog):
        engine = _make_engine()
        _arm_fan_off_grace_while_paused(engine)
        caplog.set_level(logging.DEBUG, logger=_AUTOMATION_LOGGER)

        _run_check(engine)
        _run_check(engine)

        levels = [r.levelno for r in caplog.records if "Nat-vent evaluation" in r.getMessage()]
        assert levels == [logging.INFO, logging.DEBUG]

    def test_a_changed_reason_logs_at_info_again(self, caplog):
        engine = _make_engine()
        _arm_fan_off_grace_while_paused(engine)
        caplog.set_level(logging.DEBUG, logger=_AUTOMATION_LOGGER)
        _run_check(engine)

        engine._grace_active = False
        engine._sensor_check_callback = lambda: False  # window closed: a different blocker
        _run_check(engine)

        recs = [r for r in caplog.records if "Nat-vent evaluation" in r.getMessage()]
        assert [r.levelno for r in recs] == [logging.INFO, logging.INFO]
        assert "reason=no_sensor_open" in recs[1].getMessage()

    def test_gate_rejection_after_grace_names_gate_reasons(self, caplog):
        """Grace over, window open, but outdoor is not cooler than indoor: the evaluation
        runs and the gate says no — previously silent."""
        engine = _make_engine(indoor_f=70.0)
        _arm_fan_off_grace_while_paused(engine)
        engine._grace_active = False
        engine._last_outdoor_temp = 80.0
        caplog.set_level(logging.DEBUG, logger=_AUTOMATION_LOGGER)

        _run_check(engine)

        msgs = [r.getMessage() for r in caplog.records if "Nat-vent evaluation" in r.getMessage()]
        assert len(msgs) == 1, msgs
        assert "Nat-vent evaluation not engaged" in msgs[0]
        assert "outdoor_not_cooler_than_indoor" in msgs[0]
        assert "outdoor_above_threshold" in msgs[0]
        assert engine._natural_vent_active is False


class TestNotEngagedNamesTheFsmBlocker:
    """Review finding: the FSM checks override/grace BEFORE the temperature gate, so a log that
    only listed gate reasons blamed the wrong thing (or printed a bare fsm_declined)."""

    def test_actively_paused_with_grace_names_grace_not_fsm_declined(self, caplog):
        """Window opened while HVAC was running (so it was actively paused), then a fan-off grace."""
        engine = _make_engine(indoor_f=72.0)
        _arm_fan_off_grace_while_paused(engine)
        engine._paused_with_hvac_already_off = False
        caplog.set_level(logging.DEBUG, logger=_AUTOMATION_LOGGER)

        _run_check(engine)

        msgs = [r.getMessage() for r in caplog.records if "Nat-vent evaluation" in r.getMessage()]
        assert len(msgs) == 1, msgs
        assert "Nat-vent evaluation not engaged" in msgs[0]
        assert "reason=grace_active" in msgs[0]
        assert "fsm_declined" not in msgs[0]
        assert "trigger=fan_off" in msgs[0]
        assert "ends=21:45" in msgs[0]
        assert engine._natural_vent_active is False

    def test_fan_override_is_named(self, caplog):
        engine = _make_engine(indoor_f=72.0)
        _arm_fan_off_grace_while_paused(engine)
        engine._grace_active = False
        engine._fan_override_active = True
        caplog.set_level(logging.DEBUG, logger=_AUTOMATION_LOGGER)

        _run_check(engine)

        msgs = [r.getMessage() for r in caplog.records if "Nat-vent evaluation" in r.getMessage()]
        assert len(msgs) == 1, msgs
        assert "override_active" in msgs[0]

    def test_overheat_exception_is_not_blamed_on_grace(self, caplog):
        """Indoor above comfort_cool lets nat-vent through grace (Issue #134); the log must not
        list grace_active as the reason then.

        Issue #1205: #134 is for CA's OWN graces, so this scenario now runs under an automation grace (CA's
        nat-vent-exit resume), not the occupant's fan-off hold -- see the sibling test below for the hold."""
        engine = _make_engine(indoor_f=78.0)  # comfort_cool is 74
        _arm_fan_off_grace_while_paused(engine)
        engine._last_resume_source = "automation"
        engine._last_grace_trigger = "nat_vent_exit_resume"
        engine._paused_with_hvac_already_off = False
        engine._last_outdoor_temp = 80.0  # warmer than indoor: the gate itself says no
        caplog.set_level(logging.DEBUG, logger=_AUTOMATION_LOGGER)

        _run_check(engine)

        msgs = [r.getMessage() for r in caplog.records if "Nat-vent evaluation" in r.getMessage()]
        assert len(msgs) == 1, msgs
        assert "grace_active" not in msgs[0].split("trigger=")[0]
        assert "outdoor_not_cooler_than_indoor" in msgs[0]

    def test_overheat_does_not_lift_the_fan_off_hold_and_grace_is_the_blocker(self, caplog):
        """Issue #1205: the same hot house under the OCCUPANT's fan-off hold: grace_active IS the blocker, and the
        hold-beats-ceiling line is logged once at INFO (outcome first, then key=value)."""
        engine = _make_engine(indoor_f=78.0)  # comfort_cool is 74
        _arm_fan_off_grace_while_paused(engine)
        # the hold has its own deadline (Issue #1205); this fixture writes the grace fields by hand, so it sets it too
        engine._fan_off_hold_until = datetime.fromisoformat(_GRACE_END)
        engine._paused_with_hvac_already_off = False
        engine._last_outdoor_temp = 60.0  # the gate itself would say yes
        caplog.set_level(logging.DEBUG, logger=_AUTOMATION_LOGGER)

        _run_check(engine)
        _run_check(engine)

        msgs = [r.getMessage() for r in caplog.records if "Nat-vent evaluation" in r.getMessage()]
        assert msgs and "grace_active" in msgs[0].split("trigger=")[0]
        hold = [
            r for r in caplog.records if "occupant's fan-off hold wins over comfort-ceiling bypass" in r.getMessage()
        ]
        assert len(hold) == 1 and hold[0].levelno == logging.INFO, [r.getMessage() for r in hold]
        text = hold[0].getMessage()
        assert text.startswith("Nat-vent not re-entered:")
        assert "indoor=" in text and "comfort_cool=" in text and "grace_remaining_min=" in text
        assert engine._natural_vent_active is False

    def test_paused_with_unavailable_temperature_is_logged(self, caplog):
        engine = _make_engine()
        _arm_fan_off_grace_while_paused(engine)
        engine._paused_with_hvac_already_off = False
        engine._grace_active = False
        engine._last_outdoor_temp = None
        caplog.set_level(logging.DEBUG, logger=_AUTOMATION_LOGGER)

        _run_check(engine)

        msgs = [r.getMessage() for r in caplog.records if "Nat-vent evaluation" in r.getMessage()]
        assert any("reason=temperature_unavailable" in m for m in msgs), msgs


class TestEvalLogKeyResets:
    def test_new_grace_after_a_block_logs_at_info_again(self, caplog):
        """Same blocker twice in a row is DEBUG the second time; but a NEW grace is a new event."""
        engine = _make_engine()
        _arm_fan_off_grace_while_paused(engine)
        caplog.set_level(logging.DEBUG, logger=_AUTOMATION_LOGGER)
        _run_check(engine)
        _run_check(engine)  # repeat -> DEBUG

        with (
            patch("custom_components.climate_advisor.automation.async_call_later", return_value=MagicMock()),
            patch(f"{_DT_PATH}.now", return_value=_NOW),
            patch(f"{_DT_PATH}.parse_datetime", side_effect=lambda s: datetime.fromisoformat(s)),
            patch(f"{_DT_PATH}.as_local", side_effect=lambda x: x),
        ):
            engine._start_grace_period_action("manual", trigger="fan_off", duration_override=10800)
        _run_check(engine)

        levels = [r.levelno for r in caplog.records if "Nat-vent evaluation" in r.getMessage()]
        assert levels == [logging.INFO, logging.DEBUG, logging.INFO]


# ---------------------------------------------------------------------------
# Grace start log mirrors the grace cancel log
# ---------------------------------------------------------------------------


class TestGraceStartLog:
    def test_start_line_carries_trigger_source_and_end_time(self, caplog):
        engine = _make_engine()
        caplog.set_level(logging.INFO, logger=_AUTOMATION_LOGGER)
        with (
            patch("custom_components.climate_advisor.automation.async_call_later", return_value=MagicMock()),
            patch(f"{_DT_PATH}.now", return_value=datetime(2026, 10, 11, 18, 45, 34)),
            patch(f"{_DT_PATH}.parse_datetime", side_effect=lambda s: datetime.fromisoformat(s)),
            patch(f"{_DT_PATH}.as_local", side_effect=lambda x: x),
        ):
            started = engine._start_grace_period_action("manual", trigger="fan_off", duration_override=10800)

        assert started is True
        msg = next(r.getMessage() for r in caplog.records if "grace period" in r.getMessage())
        # prefix kept so existing grep habits still match
        assert msg.startswith("Started manual grace period (10800 seconds)")
        assert "trigger=fan_off" in msg
        assert "source=manual" in msg
        assert "ends=21:45" in msg


# ---------------------------------------------------------------------------
# Ending a grace with no override behind it (dashboard "End Grace Period")
# ---------------------------------------------------------------------------


def _cancel_view_and_request(engine: AutomationEngine):
    from custom_components.climate_advisor import api as api_mod

    coordinator = MagicMock()
    coordinator.automation_engine = engine
    coordinator.zone_label = "test"
    hass = MagicMock()
    hass.async_create_task = MagicMock(side_effect=lambda c: getattr(c, "close", lambda: None)())
    request = MagicMock()
    request.app = {"hass": hass}
    return api_mod, coordinator, request


class TestEndGraceWithoutOverride:
    def test_engine_ends_a_grace_only_state_without_starting_another(self):
        engine = _make_engine()
        _arm_fan_off_grace_while_paused(engine)
        assert engine._manual_override_active is False
        assert engine._fan_override_active is False

        with patch(f"{_DT_PATH}.now", return_value=_NOW):
            cancelled = engine.cancel_override(reason="user_cancel_override")

        assert cancelled is True
        assert engine._grace_active is False
        # The door/window pause is untouched: ending grace must not resume HVAC or unpause.
        assert engine._paused_by_door is True
        engine.hass.services.async_call.assert_not_called()

    def test_nat_vent_evaluation_is_no_longer_blocked_by_grace_after_ending_it(self, caplog):
        engine = _make_engine()
        _arm_fan_off_grace_while_paused(engine)
        with patch(f"{_DT_PATH}.now", return_value=_NOW):
            engine.cancel_override(reason="user_cancel_override")
        caplog.set_level(logging.DEBUG, logger=_AUTOMATION_LOGGER)

        _run_check(engine)

        msgs = [r.getMessage() for r in caplog.records if "Nat-vent evaluation" in r.getMessage()]
        assert not any("grace_active" in m for m in msgs), msgs

    def test_view_says_grace_ended_not_override_cancelled(self):
        engine = _make_engine()
        _arm_fan_off_grace_while_paused(engine)
        api_mod, coordinator, request = _cancel_view_and_request(engine)
        view = api_mod.ClimateAdvisorCancelOverrideView()

        with (
            patch.object(api_mod, "_get_coordinator", return_value=(coordinator, False)),
            patch.object(api_mod, "_schedule_reclassify_after_cancel"),
            patch(f"{_DT_PATH}.now", return_value=_NOW),
        ):
            response = asyncio.run(view.post(request))

        assert response.json_data["message"].startswith("Grace period ended")

    def test_view_still_says_override_cancelled_when_an_override_exists(self):
        engine = _make_engine()
        _arm_fan_off_grace_while_paused(engine)
        engine._manual_override_active = True
        api_mod, coordinator, request = _cancel_view_and_request(engine)
        view = api_mod.ClimateAdvisorCancelOverrideView()

        with (
            patch.object(api_mod, "_get_coordinator", return_value=(coordinator, False)),
            patch.object(api_mod, "_schedule_reclassify_after_cancel"),
            patch(f"{_DT_PATH}.now", return_value=_NOW),
        ):
            response = asyncio.run(view.post(request))

        assert response.json_data["message"].startswith("Override cancelled")
