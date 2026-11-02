"""Issue #1142 step 1: the per-site start profiles feed the nat-vent FSM exactly the knobs each site used before.

One shell helper (``AutomationEngine._nat_vent_start_decision``) replaced four hand-built FSM-input blocks. These
tests pin each profile's knobs through the REAL helper (it builds the real ``NatVentFsmInputs`` and returns the
FSM's own transition), so changing one knob in the table changes an outcome here.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import patch

import pytest

from custom_components.climate_advisor.automation import (
    NAT_VENT_START_GRACE_RECHECK,
    NAT_VENT_START_IDLE_OPEN,
    NAT_VENT_START_PAUSED_REACTIVATION,
    NAT_VENT_START_WINDOW_OPEN,
    NatVentStartProfile,
)
from custom_components.climate_advisor.nat_vent_lifecycle import NatVentLifecycleState
from tests.test_door_pause_recorded_1132 import _AUTOMATION, _DAY, _engine

_PROFILES = [
    NAT_VENT_START_WINDOW_OPEN,
    NAT_VENT_START_GRACE_RECHECK,
    NAT_VENT_START_IDLE_OPEN,
    NAT_VENT_START_PAUSED_REACTIVATION,
]


def _decide(engine, profile, *, indoor=72.0, outdoor=65.0):
    with (
        patch(f"{_AUTOMATION}.dt_util.now", return_value=_DAY),
        patch(f"{_AUTOMATION}.dt_util.parse_datetime", side_effect=datetime.fromisoformat),
    ):
        return engine._nat_vent_start_decision(profile, indoor=indoor, outdoor=outdoor)


def _spy_inputs(engine):
    seen = {}
    real = engine._build_nat_vent_fsm_inputs

    def _spy(**kwargs):
        seen.update(kwargs)
        return real(**kwargs)

    engine._build_nat_vent_fsm_inputs = _spy
    return seen


def test_profile_table_is_the_documented_knob_set():
    assert (NAT_VENT_START_WINDOW_OPEN.hysteresis, NAT_VENT_START_WINDOW_OPEN.grace_active) == (0.0, False)
    assert NAT_VENT_START_WINDOW_OPEN.paused_by_door is None
    assert NAT_VENT_START_WINDOW_OPEN.apply_reactivation_floor is False
    assert NAT_VENT_START_WINDOW_OPEN.live_lifecycle_state is False

    assert (NAT_VENT_START_GRACE_RECHECK.hysteresis, NAT_VENT_START_GRACE_RECHECK.paused_by_door) == (0.0, False)
    assert NAT_VENT_START_GRACE_RECHECK.apply_reactivation_floor is True
    assert NAT_VENT_START_GRACE_RECHECK.live_lifecycle_state is False

    for profile in (NAT_VENT_START_IDLE_OPEN, NAT_VENT_START_PAUSED_REACTIVATION):
        assert profile.hysteresis is None and profile.grace_active is None and profile.paused_by_door is None
        assert profile.apply_reactivation_floor is True
        assert profile.live_lifecycle_state is True
        assert profile.soft_start is True


@pytest.mark.parametrize("profile", _PROFILES, ids=lambda p: p.name)
def test_helper_always_asks_with_the_start_guards(profile):
    engine = _engine()
    seen = _spy_inputs(engine)
    _decide(engine, profile)
    assert seen["start_guards"] is True
    assert seen["hysteresis"] == profile.hysteresis
    assert seen["grace_active"] == profile.grace_active
    assert seen["paused_by_door"] == profile.paused_by_door
    assert seen["apply_reactivation_floor"] == profile.apply_reactivation_floor


def test_fresh_open_profile_ignores_the_live_grace_flag():
    engine = _engine()
    engine._grace_active = True  # the live flag would block the FSM (grace blocks nat-vent at 72F <= comfort_cool)
    assert _decide(engine, NAT_VENT_START_WINDOW_OPEN).to_state == NatVentLifecycleState.ACTIVE_FULL_GATE
    assert _decide(engine, NAT_VENT_START_IDLE_OPEN).to_state == NatVentLifecycleState.INACTIVE


def test_reactivation_profile_applies_the_lockout_but_the_grace_recheck_does_not():
    engine = _engine()
    engine._paused_by_door = True
    engine._nat_vent_outdoor_exit_time = _DAY  # exit "just now": inside the lockout
    assert (
        _decide(engine, NAT_VENT_START_PAUSED_REACTIVATION).to_state
        == NatVentLifecycleState.PAUSED_REACTIVATION_LOCKOUT
    )
    assert _decide(engine, NAT_VENT_START_GRACE_RECHECK).to_state == NatVentLifecycleState.ACTIVE_FULL_GATE


def test_soft_start_profile_flag_and_config_switch():
    engine = _engine(mode="cool", indoor=80.0, outdoor=79.0)
    engine._outdoor_temp_today_peak = 85.0
    engine._outdoor_temp_today_sample_count = 20
    kwargs = {"indoor": 80.0, "outdoor": 79.0}
    assert _decide(engine, NAT_VENT_START_IDLE_OPEN, **kwargs).to_state == NatVentLifecycleState.ACTIVE_SOFT_START
    no_soft = NatVentStartProfile("no_soft", soft_start=False, live_lifecycle_state=True)
    assert _decide(engine, no_soft, **kwargs).to_state == NatVentLifecycleState.INACTIVE
    engine.config["nat_vent_soft_start_enabled"] = False
    assert _decide(engine, NAT_VENT_START_IDLE_OPEN, **kwargs).to_state == NatVentLifecycleState.INACTIVE


def test_helper_returns_the_veto_when_a_start_rule_refuses():
    engine = _engine()
    engine._hourly_forecast_temps = [{"temperature": 95.0, "datetime": "2026-10-28T15:00:00+00:00"}]
    transition = _decide(engine, NAT_VENT_START_WINDOW_OPEN)
    assert transition.to_state == NatVentLifecycleState.INACTIVE
    assert transition.veto is not None


def test_utc_fixture_sanity():
    assert _DAY.tzinfo == UTC
