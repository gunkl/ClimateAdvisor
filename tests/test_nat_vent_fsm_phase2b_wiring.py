"""Tests for Issue #694 (epic #594 Phase R, Phase 2b): wiring the 4 remaining
real nat-vent decision points through ``nat_vent_fsm.transition()`` when
``AutomationEngine._natvent_fsm_authoritative`` is True:

  1. ``handle_door_window_open()``'s entry gate
  2. ``check_natural_vent_conditions()``'s idle-open re-entry (full-gate + soft-start)
  3. ``check_natural_vent_conditions()``'s reactivation-while-paused (lockout +
     full-gate + soft-start)
  4. ``reconcile_fan_on_startup()``'s adopt-on eligibility gate

Each site is exercised both with the flag True (routes through the FSM) and
False (legacy gate functions) to prove the two paths still agree — a
revert-test in spirit: flipping the flag must not change what the real engine
does for these scenarios. Real ``AutomationEngine`` methods are invoked
directly (no re-implementation of the decision logic under test), per this
project's doctrine against mirror tests.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from custom_components.climate_advisor.automation import AutomationEngine
from custom_components.climate_advisor.const import (
    CONF_FAN_MODE,
    CONF_NAT_VENT_SOFT_START_ENABLED,
    FAN_MODE_WHOLE_HOUSE,
)

_NOW = datetime(2026, 7, 15, 14, 0, 0)
sys.modules["homeassistant.util.dt"].now = lambda: _NOW

import custom_components.climate_advisor.automation as _automation_mod  # noqa: E402

# automation.py's own `dt_util` reference is a child mock of homeassistant.util,
# distinct from sys.modules["homeassistant.util.dt"] — tests that exercise the
# reactivation-lockout datetime math (elapsed = now - outdoor_exit_time) must
# patch this exact path, mirroring test_nat_vent_activation.py's _DT_NOW_PATH.
_DT_NOW_PATH = "custom_components.climate_advisor.automation.dt_util.now"


def _real_parse_datetime(dt_str: str):
    try:
        return datetime.fromisoformat(dt_str)
    except Exception:
        return None


_automation_mod.dt_util.parse_datetime = _real_parse_datetime


def _make_engine(
    *,
    comfort_heat: float = 68.0,
    comfort_cool: float = 74.0,
    nat_vent_delta: float = 3.0,
    hysteresis: float = 1.0,
    indoor_f: float | None = None,
    fan_mode: str = FAN_MODE_WHOLE_HOUSE,
    soft_start_enabled: bool = True,
    authoritative: bool = False,
) -> AutomationEngine:
    """Create a real AutomationEngine with mocked HA dependencies."""
    hass = MagicMock()
    hass.services = MagicMock()
    hass.services.async_call = AsyncMock()

    def _consume_coroutine(coro):
        coro.close()

    hass.async_create_task = MagicMock(side_effect=_consume_coroutine)

    climate_state = MagicMock()
    climate_state.state = "cool"
    climate_state.attributes = {} if indoor_f is None else {"current_temperature": indoor_f}
    hass.states = MagicMock()
    hass.states.get = MagicMock(return_value=climate_state)

    config = {
        "comfort_heat": comfort_heat,
        "comfort_cool": comfort_cool,
        "setback_heat": 60,
        "setback_cool": 80,
        "natural_vent_delta": nat_vent_delta,
        "nat_vent_hysteresis_f": hysteresis,
        "notify_service": "notify.notify",
        CONF_FAN_MODE: fan_mode,
        CONF_NAT_VENT_SOFT_START_ENABLED: soft_start_enabled,
    }

    engine = AutomationEngine(
        hass=hass,
        climate_entity="climate.thermostat",
        weather_entity="weather.forecast_home",
        door_window_sensors=["binary_sensor.front_door"],
        notify_service="notify.notify",
        config=config,
    )
    engine._natvent_fsm_authoritative = authoritative
    return engine


# ---------------------------------------------------------------------------
# Site 1: handle_door_window_open()
# ---------------------------------------------------------------------------


class TestSite1DoorWindowOpenEntry:
    """Full-gate entry via handle_door_window_open() — FSM-authoritative vs legacy."""

    def _run(self, *, authoritative: bool, outdoor: float, indoor: float) -> AutomationEngine:
        engine = _make_engine(indoor_f=indoor, authoritative=authoritative)
        engine._last_outdoor_temp = outdoor
        asyncio.run(engine.handle_door_window_open("binary_sensor.front_door"))
        return engine

    def test_authoritative_activates_when_gate_clears(self):
        # outdoor 60 well below indoor 72 -> decide_nat_vent_gate() clears easily.
        engine = self._run(authoritative=True, outdoor=60.0, indoor=72.0)
        assert engine._natural_vent_active is True
        assert engine._paused_by_door is False

    def test_legacy_activates_when_gate_clears(self):
        engine = self._run(authoritative=False, outdoor=60.0, indoor=72.0)
        assert engine._natural_vent_active is True
        assert engine._paused_by_door is False

    def test_authoritative_and_legacy_agree_when_gate_fails(self):
        # outdoor(75) > indoor(72) -> directional guard fails both ways -> pause.
        for authoritative in (True, False):
            engine = self._run(authoritative=authoritative, outdoor=75.0, indoor=72.0)
            assert engine._natural_vent_active is False, f"authoritative={authoritative}"
            assert engine._paused_by_door is True, f"authoritative={authoritative}"

    def test_authoritative_uses_zero_hysteresis_matching_legacy(self):
        """This call site is one of only 2 (of 5) _nat_vent_may_reactivate() callers
        that always uses hysteresis=0.0, not the configured value (here 1.0F).
        outdoor=69.5, indoor=70.0: with hysteresis=0 the gate clears (69.5 < 70);
        with the configured 1.0F hysteresis it would NOT (69.5 is not < 69.0). Both
        paths must activate — proving the FSM branch was fed hysteresis=0.0, not
        the configured value _build_nat_vent_fsm_inputs() would otherwise supply."""
        for authoritative in (True, False):
            engine = self._run(authoritative=authoritative, outdoor=69.5, indoor=70.0)
            assert engine._natural_vent_active is True, (
                f"authoritative={authoritative}: hysteresis must be 0.0 at this call site"
            )

    def test_authoritative_never_enters_soft_start_at_this_site(self):
        """This call site has never modeled soft-start entry (no
        _nat_vent_may_soft_start() call here) -- Phase 2b is wiring-only, so an
        FSM-produced ACTIVE_SOFT_START result must be treated as "not entered"
        here, same as legacy. outdoor==indoor (parity) satisfies the soft-start
        sub-gate but not the full bulk-cooling gate."""
        engine = _make_engine(
            comfort_heat=68.0,
            comfort_cool=74.0,
            indoor_f=76.0,
            authoritative=True,
        )
        engine._last_outdoor_temp = 76.0  # parity, not below indoor
        engine._outdoor_temp_today_peak = 90.0
        engine._outdoor_temp_today_sample_count = 5
        asyncio.run(engine.handle_door_window_open("binary_sensor.front_door"))
        assert engine._natural_vent_active is False
        assert engine._nat_vent_soft_start is False
        assert engine._paused_by_door is True

    def test_authoritative_keeps_in_flight_soft_start_session_running(self):
        """Defect 1/2 regression (Issue #694 Verification): a second door/window
        opening during an already-active soft-start nat-vent session must not
        kill the session (routing an entry-gate question through the FSM's exit
        chain via a live nat_vent_lifecycle_state) nor silently promote it to
        full-gate (hardcoding ACTIVE_FULL_GATE regardless of the FSM result).
        Legacy at this call site only ever writes `_natural_vent_active = True`,
        leaving `_nat_vent_soft_start` alone -- the fix must reproduce exactly
        that flag-for-flag outcome."""
        engine = _make_engine(
            comfort_heat=68.0,
            comfort_cool=74.0,
            indoor_f=75.0,  # at/above comfort_cool
            authoritative=True,
        )
        engine._occupancy_mode = "away"
        engine._natural_vent_active = True
        engine._nat_vent_soft_start = True
        engine._paused_by_door = False
        engine._last_outdoor_temp = 60.0  # favorable for free cooling
        asyncio.run(engine.handle_door_window_open("binary_sensor.front_door"))
        assert engine._natural_vent_active is True
        assert engine._nat_vent_soft_start is True, (
            "in-flight soft-start session must not be silently promoted to full-gate"
        )
        assert engine._paused_by_door is False


# ---------------------------------------------------------------------------
# Site 2: check_natural_vent_conditions() idle-open re-entry
# ---------------------------------------------------------------------------


class TestSite2IdleOpenReentry:
    def _arm_idle_open(self, engine: AutomationEngine, *, outdoor: float) -> None:
        engine._paused_by_door = False
        engine._natural_vent_active = False
        engine._nat_vent_soft_start = False
        engine._grace_active = False
        engine._sensor_check_callback = lambda: True
        engine._last_outdoor_temp = outdoor
        engine.hass.states.get.return_value.state = "off"

    def test_authoritative_full_gate_reactivates(self):
        for authoritative in (True, False):
            engine = _make_engine(comfort_heat=68.0, comfort_cool=74.0, indoor_f=72.0, authoritative=authoritative)
            self._arm_idle_open(engine, outdoor=68.0)
            asyncio.run(engine.check_natural_vent_conditions())
            assert engine._natural_vent_active is True, f"authoritative={authoritative}"
            assert engine._nat_vent_soft_start is False, f"authoritative={authoritative}"

    def test_authoritative_soft_start_reactivates_when_full_gate_not_cleared(self):
        for authoritative in (True, False):
            engine = _make_engine(comfort_heat=68.0, comfort_cool=74.0, indoor_f=76.0, authoritative=authoritative)
            self._arm_idle_open(engine, outdoor=76.0)  # parity, past peak below
            engine._outdoor_temp_today_peak = 90.0
            engine._outdoor_temp_today_sample_count = 5
            asyncio.run(engine.check_natural_vent_conditions())
            assert engine._natural_vent_active is True, f"authoritative={authoritative}"
            assert engine._nat_vent_soft_start is True, f"authoritative={authoritative}"

    def test_authoritative_respects_soft_start_disabled_config(self):
        """CONF_NAT_VENT_SOFT_START_ENABLED has no FSM-side field -- the FSM's pure
        decide_nat_vent_soft_start_gate() always evaluates the condition. Phase 2b's
        caller-side post-filter must suppress the ACTIVE_SOFT_START result when the
        user has this feature disabled, matching legacy's own opt-out check inside
        _nat_vent_may_soft_start()."""
        engine = _make_engine(
            comfort_heat=68.0,
            comfort_cool=74.0,
            indoor_f=76.0,
            soft_start_enabled=False,
            authoritative=True,
        )
        self._arm_idle_open(engine, outdoor=76.0)
        engine._outdoor_temp_today_peak = 90.0
        engine._outdoor_temp_today_sample_count = 5
        asyncio.run(engine.check_natural_vent_conditions())
        assert engine._natural_vent_active is False
        assert engine._nat_vent_soft_start is False

    def test_authoritative_and_legacy_agree_when_neither_gate_clears(self):
        for authoritative in (True, False):
            engine = _make_engine(comfort_heat=68.0, comfort_cool=74.0, indoor_f=72.0, authoritative=authoritative)
            self._arm_idle_open(engine, outdoor=80.0)  # outdoor far above indoor
            asyncio.run(engine.check_natural_vent_conditions())
            assert engine._natural_vent_active is False, f"authoritative={authoritative}"

    def test_authoritative_now_respects_lockout(self):
        """Issue #696 (supersedes the pre-fix test this replaces, which was named
        test_authoritative_ignores_stale_paused_by_door_lockout and asserted the bug
        itself as correct behavior): a real production incident (2026-08-23, see
        tools/simulations/pending/issue_696_idle_open_reactivation_bypasses_lockout.json)
        showed this site DOES need the lockout -- _paused_with_hvac_already_off=True
        (HVAC already off, nothing to interrupt) makes _actively_paused False even
        though _paused_by_door is True, which is exactly what a COMFORT_FLOOR exit
        with an open sensor produces. Both the FSM branch (no longer overriding
        paused_by_door=False when building FSM inputs) and the legacy branch (new
        explicit is_reactivation_locked_out() guard) must now block reactivation here
        while within the lockout window, matching the paused-reactivation site's
        existing behavior (TestSite3PausedReactivation below)."""
        for authoritative in (True, False):
            engine = _make_engine(comfort_heat=68.0, comfort_cool=74.0, indoor_f=69.0, authoritative=authoritative)
            self._arm_idle_open(engine, outdoor=58.6)
            engine._paused_by_door = True
            engine._paused_with_hvac_already_off = True
            engine._nat_vent_outdoor_exit_time = _NOW - timedelta(seconds=5)  # well within the 300s lockout
            with patch(_DT_NOW_PATH, return_value=_NOW):
                asyncio.run(engine.check_natural_vent_conditions())
            assert engine._natural_vent_active is False, (
                f"authoritative={authoritative}: idle-open re-entry must be blocked by the "
                "reactivation lockout within its window (Issue #696)"
            )

    def test_authoritative_preserves_paused_by_door_flag(self):
        """Defect 3 regression (Issue #694 Verification): _apply_nat_vent_fsm_state()'s
        projection unconditionally clears _paused_by_door, but legacy's write at this
        site (`self._natural_vent_active = True`) never touched it. Entering with
        _paused_by_door=True + _paused_with_hvac_already_off=True must leave
        _paused_by_door=True afterward -- otherwise the pair becomes incoherent
        (_paused_by_door=False while _paused_with_hvac_already_off=True).

        Issue #696: exit_time moved outside the (now-enforced) 300s lockout window so
        reactivation actually proceeds here -- this test is specifically about what
        happens to _paused_by_door once a genuine post-lockout reactivation succeeds,
        not about the lockout gate itself (covered separately above).

        Issue #775: indoor bumped from 69 to 71 -- this site's daytime reactivation
        floor is now (comfort_heat+comfort_cool)/2-hysteresis = (68+74)/2-1 = 70, so 69
        would now be blocked by the floor itself rather than reaching the
        paused_by_door-preservation behavior under test."""
        for authoritative in (True, False):
            engine = _make_engine(comfort_heat=68.0, comfort_cool=74.0, indoor_f=71.0, authoritative=authoritative)
            self._arm_idle_open(engine, outdoor=58.6)
            engine._paused_by_door = True
            engine._paused_with_hvac_already_off = True
            engine._nat_vent_outdoor_exit_time = _NOW - timedelta(seconds=400)  # past the 300s lockout
            with patch(_DT_NOW_PATH, return_value=_NOW):
                asyncio.run(engine.check_natural_vent_conditions())
            assert engine._natural_vent_active is True, f"authoritative={authoritative}"
            assert engine._paused_by_door is True, (
                f"authoritative={authoritative}: _paused_by_door must survive the FSM apply"
            )


# ---------------------------------------------------------------------------
# Site 3: check_natural_vent_conditions() reactivation-while-paused
# ---------------------------------------------------------------------------


class TestSite3PausedReactivation:
    def _arm_paused(self, engine: AutomationEngine, *, outdoor: float, exit_time: datetime | None = None) -> None:
        engine._paused_by_door = True
        engine._paused_with_hvac_already_off = False  # -> _actively_paused True, reaches site 3
        engine._natural_vent_active = False
        engine._nat_vent_soft_start = False
        engine._last_outdoor_temp = outdoor
        engine._nat_vent_outdoor_exit_time = exit_time

    def test_authoritative_full_gate_reactivates_while_paused(self):
        for authoritative in (True, False):
            engine = _make_engine(comfort_heat=68.0, comfort_cool=74.0, indoor_f=72.0, authoritative=authoritative)
            self._arm_paused(engine, outdoor=68.0)
            asyncio.run(engine.check_natural_vent_conditions())
            assert engine._natural_vent_active is True, f"authoritative={authoritative}"
            assert engine._paused_by_door is False, f"authoritative={authoritative}"

    def test_authoritative_soft_start_reactivates_while_paused(self):
        for authoritative in (True, False):
            engine = _make_engine(comfort_heat=68.0, comfort_cool=74.0, indoor_f=76.0, authoritative=authoritative)
            self._arm_paused(engine, outdoor=76.0)
            engine._outdoor_temp_today_peak = 90.0
            engine._outdoor_temp_today_sample_count = 5
            asyncio.run(engine.check_natural_vent_conditions())
            assert engine._natural_vent_active is True, f"authoritative={authoritative}"
            assert engine._nat_vent_soft_start is True, f"authoritative={authoritative}"
            assert engine._paused_by_door is False, f"authoritative={authoritative}"

    def test_authoritative_lockout_blocks_reactivation(self):
        """Issue #641: a proactive-floor exit arms a 300s reactivation lockout via
        _nat_vent_outdoor_exit_time. Both paths must stay paused through the very
        next tick even though the instantaneous gate would otherwise clear."""
        for authoritative in (True, False):
            engine = _make_engine(comfort_heat=68.0, comfort_cool=74.0, indoor_f=69.0, authoritative=authoritative)
            self._arm_paused(engine, outdoor=58.6, exit_time=_NOW - timedelta(seconds=5))
            with patch(_DT_NOW_PATH, return_value=_NOW):
                asyncio.run(engine.check_natural_vent_conditions())
            assert engine._natural_vent_active is False, f"authoritative={authoritative}"
            assert engine._paused_by_door is True, f"authoritative={authoritative}"

    def test_authoritative_lockout_expires_and_reactivates(self):
        """Issue #775: indoor bumped from 69 to 71 -- this site's daytime reactivation
        floor is now (comfort_heat+comfort_cool)/2-hysteresis = (68+74)/2-1 = 70, so 69
        would now be blocked by the floor itself rather than isolating the lockout
        expiry behavior under test."""
        for authoritative in (True, False):
            engine = _make_engine(comfort_heat=68.0, comfort_cool=74.0, indoor_f=71.0, authoritative=authoritative)
            self._arm_paused(engine, outdoor=58.6, exit_time=_NOW - timedelta(seconds=301))
            with patch(_DT_NOW_PATH, return_value=_NOW):
                asyncio.run(engine.check_natural_vent_conditions())
            assert engine._natural_vent_active is True, f"authoritative={authoritative}"
            assert engine._paused_by_door is False, f"authoritative={authoritative}"

    def test_authoritative_and_legacy_agree_when_gate_fails(self):
        for authoritative in (True, False):
            engine = _make_engine(comfort_heat=68.0, comfort_cool=74.0, indoor_f=72.0, authoritative=authoritative)
            self._arm_paused(engine, outdoor=80.0)
            asyncio.run(engine.check_natural_vent_conditions())
            assert engine._natural_vent_active is False, f"authoritative={authoritative}"
            assert engine._paused_by_door is True, f"authoritative={authoritative}"


# ---------------------------------------------------------------------------
# Site 4: reconcile_fan_on_startup()
# ---------------------------------------------------------------------------


class TestSite4ReconcileFanOnStartup:
    def _run_reconcile(
        self, engine: AutomationEngine, *, indoor: float, outdoor: float, any_sensor_open: bool = True
    ) -> None:
        asyncio.run(
            engine.reconcile_fan_on_startup(
                indoor=indoor,
                outdoor=outdoor,
                thermostat_fan_running=True,
                any_sensor_open=any_sensor_open,
                trigger="ha_restart",
            )
        )

    def test_authoritative_adopts_when_gate_clears(self):
        for authoritative in (True, False):
            engine = _make_engine(comfort_heat=68.0, comfort_cool=74.0, authoritative=authoritative)
            self._run_reconcile(engine, indoor=72.0, outdoor=68.0)
            assert engine._natural_vent_active is True, f"authoritative={authoritative}"

    def test_authoritative_turns_off_when_gate_fails(self):
        for authoritative in (True, False):
            engine = _make_engine(comfort_heat=68.0, comfort_cool=74.0, authoritative=authoritative)
            engine._exit_nat_vent = AsyncMock()
            self._run_reconcile(engine, indoor=72.0, outdoor=80.0)
            engine._exit_nat_vent.assert_awaited()

    def test_authoritative_never_adopts_via_soft_start_at_this_site(self):
        """Like handle_door_window_open(), this call site has never modeled
        soft-start adoption -- an FSM-produced ACTIVE_SOFT_START must be treated
        as "not eligible", not silently adopted as a full session."""
        engine = _make_engine(comfort_heat=68.0, comfort_cool=74.0, authoritative=True)
        engine._outdoor_temp_today_peak = 90.0
        engine._outdoor_temp_today_sample_count = 5
        engine._exit_nat_vent = AsyncMock()
        self._run_reconcile(engine, indoor=76.0, outdoor=76.0)  # parity only -> soft-start territory
        assert engine._natural_vent_active is False
        engine._exit_nat_vent.assert_awaited()

    def test_authoritative_ignores_stale_paused_by_door_state(self):
        """This is a pure entry-gate question independent of restored/stale
        _paused_by_door state at reconcile time -- legacy never consults it here,
        and the FSM path must not either."""
        for authoritative in (True, False):
            engine = _make_engine(comfort_heat=68.0, comfort_cool=74.0, authoritative=authoritative)
            engine._paused_by_door = True
            engine._nat_vent_outdoor_exit_time = _NOW - timedelta(seconds=5)
            self._run_reconcile(engine, indoor=72.0, outdoor=68.0)
            assert engine._natural_vent_active is True, f"authoritative={authoritative}"
