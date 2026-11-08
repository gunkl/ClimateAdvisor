"""Tests for Issue #87 — window/HVAC interaction fixes.

Four fixes are validated here:

Fix 1: check_window_cooling_opportunity() returns False when _natural_vent_active is
       True — prevents the economizer from restarting AC while nat-vent is already
       handling cooling.

Fix 2: handle_door_window_open() allows nat-vent to bypass an active grace period
       when outdoor temperature is at or below the nat-vent threshold.

Fix 3: _re_pause_for_open_sensor() checks nat-vent conditions before re-pausing when
       a grace period expires with sensors still open.

Fix 4: _start_grace_period() sets _grace_end_time to a valid ISO timestamp; grace
       expiry clears it back to None.
"""

from __future__ import annotations

import asyncio
import types
from datetime import datetime, time
from unittest.mock import AsyncMock, MagicMock, patch

from custom_components.climate_advisor import automation as _ae_mod
from custom_components.climate_advisor.automation import AutomationEngine
from custom_components.climate_advisor.classifier import DayClassification
from custom_components.climate_advisor.const import OCCUPANCY_HOME
from custom_components.climate_advisor.engine_inputs import StaticEngineInputs  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_hot_classification() -> DayClassification:
    """Minimal hot-day DayClassification."""
    c = object.__new__(DayClassification)
    c.__dict__.update(
        {
            "day_type": "hot",
            "trend_direction": "stable",
            "trend_magnitude": 0,
            "today_high": 90,
            "today_low": 65,
            "tomorrow_high": 88,
            "tomorrow_low": 64,
            "hvac_mode": "cool",
            "pre_condition": True,
            "pre_condition_target": -2.0,
            "windows_recommended": False,
            "window_open_time": None,
            "window_close_time": None,
            "setback_modifier": 0.0,
            "window_opportunity_morning": False,
            "window_opportunity_evening": True,
        }
    )
    return c


def _make_ae_stub(**overrides) -> AutomationEngine:
    """Build a minimal AutomationEngine stub using object.__new__.

    All HA async methods are replaced with AsyncMock / MagicMock so tests do
    not require a running Home Assistant instance.  The real methods under test
    are bound via types.MethodType so they execute against the stub's state.
    """
    hass = MagicMock()
    hass.async_create_task = MagicMock(side_effect=lambda coro: coro.close())
    hass.states = MagicMock()

    ae = object.__new__(AutomationEngine)
    ae.hass = hass
    ae.climate_entity = "climate.thermostat"
    ae.config = {
        "comfort_cool": 75,
        "comfort_heat": 68,
        "temp_unit": "fahrenheit",
        "natural_vent_delta": 3,  # threshold = 75 + 3 = 78 °F
        "aggressive_savings": False,
        "economizer_temp_delta": 3,
        "sensor_debounce_seconds": 0,
        "manual_grace_period": 1800,
        "automation_grace_period": 300,
        "manual_grace_notify": False,
        "automation_grace_notify": False,
        # Issue #1140: nat-vent needs a working fan
        "fan_mode": "whole_house_fan",
        "fan_entity": "fan.whf",
    }
    ae._current_classification = _make_hot_classification()
    ae._natural_vent_active = False
    ae._nat_vent_soft_start = False
    ae._inputs = StaticEngineInputs()  # Issue #1443: the facts the engine reads (empty forecast/model/peak)
    ae._economizer_active = False
    ae._economizer_phase = "inactive"
    ae._paused_by_door = False
    ae._paused_with_hvac_already_off = False
    ae._pre_pause_mode = None
    ae._doorwindow_fsm_authoritative = False
    ae._natvent_fsm_authoritative = False
    # Issue #757 Phase 6 Step 5: nat-vent's 10 inline dispatch sites are now
    # unconditionally FSM-authoritative, so _build_nat_vent_fsm_inputs() always
    # reads _occupancy_mode — previously only exercised when a test explicitly
    # set _natvent_fsm_authoritative=True.
    ae._occupancy_mode = OCCUPANCY_HOME
    ae._nat_vent_outdoor_exit_time = None
    ae._grace_active = False
    ae._grace_churn_starts = {}  # Issue #1104: grace_churn tracking (set by __init__ in production)
    ae._grace_churn_last_emit = {}
    ae._grace_protects_override = False
    ae._override_confirm_pending = False
    ae._grace_end_time = None
    ae._last_resume_source = None
    ae._last_grace_trigger = None
    ae._last_outdoor_temp = 65.0
    ae._get_indoor_temp_f = MagicMock(return_value=75.0)
    ae._fan_active = False
    ae._fan_on_since = None
    ae._manual_grace_cancel = None
    ae._automation_grace_cancel = None
    ae._sensor_check_callback = None
    ae._emit_event_callback = None
    # Issue #757 Phase 6 Step 3: _resolve_override_grace_fsm_state() is now
    # unconditionally FSM-authoritative, so _build_override_grace_fsm_inputs() always
    # reads these fields — previously only exercised when a test explicitly set
    # _override_grace_fsm_authoritative=True.
    ae._override_confirm_source = None
    ae._override_confirm_mode = None
    ae._manual_override_active = False
    ae._manual_override_mode = None
    ae._manual_override_source = None
    ae._fan_override_active = False
    ae._request_refresh_callback = None
    ae._post_grace_fan_check_callback = None
    ae.dry_run = False
    ae._decision_lock = asyncio.Lock()
    # Issue #757 Phase 6 Step 4: door/window's dispatcher is now unconditionally
    # FSM-authoritative, so every _resolve_door_window_pause_flags() call always
    # builds real DoorWindowFsmInputs via _build_door_window_fsm_inputs() — these
    # fields were previously only exercised when a test explicitly set
    # _doorwindow_fsm_authoritative=True.
    from custom_components.climate_advisor.lifecycle_dispatcher import LifecycleDispatcher

    ae._sensor_debounce_pending_callback = None
    ae._lifecycle_dispatcher = LifecycleDispatcher()
    ae._fan_drift_tick_count = 0
    ae._fan_remote_timer_hours = None
    ae._fan_min_runtime_active = False
    ae._pre_fan_hvac_mode = None
    ae._fan_rate_limited_until = None
    ae._fan_rate_limited_direction = None

    # Mock async service calls
    ae._set_hvac_mode = AsyncMock()
    ae._set_temperature = AsyncMock()
    ae._activate_fan = AsyncMock()
    ae._deactivate_fan = AsyncMock()
    ae._deactivate_economizer = AsyncMock()
    ae._notify = AsyncMock()
    ae._is_within_planned_window_period = MagicMock(return_value=False)

    for k, v in overrides.items():
        setattr(ae, k, v)

    # Bind the real methods under test
    ae.check_window_cooling_opportunity = types.MethodType(
        _ae_mod.AutomationEngine.check_window_cooling_opportunity, ae
    )
    ae.handle_door_window_open = types.MethodType(_ae_mod.AutomationEngine.handle_door_window_open, ae)
    ae._re_pause_for_open_sensor = types.MethodType(_ae_mod.AutomationEngine._re_pause_for_open_sensor, ae)
    ae._start_grace_period = types.MethodType(_ae_mod.AutomationEngine._start_grace_period, ae)
    ae._cancel_grace_timers = types.MethodType(_ae_mod.AutomationEngine._cancel_grace_timers, ae)
    ae.clear_manual_override = MagicMock()
    ae._clear_manual_override_active = MagicMock()

    return ae


# ---------------------------------------------------------------------------
# Fix 1: Economizer nat-vent guard
# ---------------------------------------------------------------------------


class TestEconomizerNatVentGuard:
    """check_window_cooling_opportunity() must not activate AC while nat-vent is active."""

    def test_economizer_does_not_activate_when_nat_vent_active(self):
        """Returns False and does NOT call _set_hvac_mode when nat-vent is running."""
        ae = _make_ae_stub(_natural_vent_active=True, _economizer_active=False)

        result = asyncio.run(
            ae.check_window_cooling_opportunity(
                outdoor_temp=70.0,
                indoor_temp=78.0,
                windows_physically_open=True,
                current_hour=19,
            )
        )

        assert result is False
        # _set_hvac_mode must NOT have been called with "cool"
        for call in ae._set_hvac_mode.call_args_list:
            assert call.args[0] != "cool", "Should not activate AC when nat-vent is active"

    def test_economizer_activates_normally_without_nat_vent(self):
        """Issue #264: cool-down activates (fan assist) without overriding the band. The #249 band
        already holds comfort_cool, so the economizer no longer calls _set_hvac_mode('cool')."""
        ae = _make_ae_stub(_natural_vent_active=False, _economizer_active=False)

        result = asyncio.run(
            ae.check_window_cooling_opportunity(
                outdoor_temp=70.0,
                indoor_temp=78.0,  # indoor > comfort_cool (75) → Phase 1 cool-down
                windows_physically_open=True,
                current_hour=19,
            )
        )

        assert result is True
        assert ae._economizer_phase == "cool-down"
        ae._set_hvac_mode.assert_not_called()  # #264: must not override the band's mode
        ae._activate_fan.assert_called_once()  # assists with the fan instead

    def test_economizer_nat_vent_guard_does_not_deactivate_economizer(self):
        """When nat-vent is active and economizer was also running, deactivation is NOT triggered.

        The nat-vent guard returns False early (before the deactivation branch that
        fires when day_type is not hot or conditions are not eligible), so
        _deactivate_economizer should NOT be called.
        """
        ae = _make_ae_stub(_natural_vent_active=True, _economizer_active=True)

        asyncio.run(
            ae.check_window_cooling_opportunity(
                outdoor_temp=70.0,
                indoor_temp=78.0,
                windows_physically_open=True,
                current_hour=19,
            )
        )

        ae._deactivate_economizer.assert_not_called()


# ---------------------------------------------------------------------------
# Fix 2: Grace-period nat-vent bypass
# ---------------------------------------------------------------------------


class TestGraceNatVentBypass:
    """handle_door_window_open() should activate nat-vent through an active grace period
    when outdoor temperature is cool enough."""

    def test_grace_active_outdoor_cool_nat_vent_activates(self):
        """Grace active + outdoor ≤ threshold → nat-vent activates, NOT blocked.

        #249: nat-vent activation no longer calls set_hvac_mode("off") — the comfort
        band stays armed and the compressor self-arbitrates with the open window.
        """
        ae = _make_ae_stub(
            _grace_active=True,
            _last_resume_source="automation",
            _last_outdoor_temp=70.0,  # below threshold of 78 °F
        )

        asyncio.run(ae.handle_door_window_open("binary_sensor.front_window"))

        # #249: nat-vent no longer sets HVAC off; band stays armed, fan turns on
        assert ae._natural_vent_active is True
        for call in ae._set_hvac_mode.call_args_list:
            assert call.args[0] != "off", "nat-vent must not call set_hvac_mode('off') under #249 band model"
        # Must NOT have set the pause flag
        assert ae._paused_by_door is False

    def test_grace_active_outdoor_warm_nat_vent_blocked(self):
        """Grace active + outdoor > threshold → blocked, no HVAC change, no pause."""
        ae = _make_ae_stub(
            _grace_active=True,
            _last_resume_source="manual",  # occupant-side grace (#1142: an automation grace no longer holds)
            _last_outdoor_temp=82.0,  # above threshold of 78 °F
        )

        asyncio.run(ae.handle_door_window_open("binary_sensor.front_window"))

        ae._set_hvac_mode.assert_not_called()
        assert ae._paused_by_door is False

    def test_grace_active_outdoor_none_nat_vent_blocked(self):
        """Grace active + no outdoor data → blocked (safe fallback)."""
        ae = _make_ae_stub(
            _grace_active=True,
            _last_resume_source="manual",  # occupant-side grace (#1142: an automation grace no longer holds)
            _last_outdoor_temp=None,
        )

        asyncio.run(ae.handle_door_window_open("binary_sensor.front_window"))

        ae._set_hvac_mode.assert_not_called()
        assert ae._paused_by_door is False


# ---------------------------------------------------------------------------
# Fix 3: _re_pause_for_open_sensor nat-vent check
# ---------------------------------------------------------------------------


class TestRePauseNatVent:
    """_re_pause_for_open_sensor() should activate nat-vent instead of pausing when
    outdoor conditions are favourable."""

    def test_re_pause_activates_nat_vent_when_outdoor_cool(self):
        """Outdoor ≤ threshold → nat-vent activated; paused_by_door stays False.

        #249: nat-vent activation no longer calls set_hvac_mode("off") — the comfort
        band stays armed; only the fan turns on and _natural_vent_active is set.
        """
        ae = _make_ae_stub(_last_outdoor_temp=70.0)

        asyncio.run(ae._re_pause_for_open_sensor())

        assert ae._natural_vent_active is True
        assert ae._paused_by_door is False
        # #249: no HVAC off call — band stays armed, fan activates instead
        for call in ae._set_hvac_mode.call_args_list:
            assert call.args[0] != "off", "nat-vent must not call set_hvac_mode('off') under #249 band model"
        ae._activate_fan.assert_called_once()

    def test_re_pause_pauses_when_outdoor_warm(self):
        """Outdoor > threshold → fall through to regular re-pause.

        Issue #757 Phase 6 Step 4: door/window's dispatcher is now unconditionally
        FSM-authoritative, so _re_pause_for_open_sensor()'s reactivation decision
        reads the already-applied _paused_by_door flag instead of independently
        recomputing the gate — by design, matching _on_grace_expired()'s own RE_PAUSE
        dispatch, which this direct-call test bypasses. _paused_by_door=True is the
        realistic precondition _on_grace_expired() would have already set for a
        blocked-reactivation outcome (RE_PAUSE -> a paused sub-state) before
        scheduling this task.
        """
        ae = _make_ae_stub(_last_outdoor_temp=82.0)
        ae._paused_by_door = True

        # Provide a mock thermostat state of "cool" so the re-pause logic fires
        mock_state = MagicMock()
        mock_state.state = "cool"
        ae.hass.states.get.return_value = mock_state

        asyncio.run(ae._re_pause_for_open_sensor())

        assert ae._paused_by_door is True
        assert ae._natural_vent_active is False
        ae._set_hvac_mode.assert_called_once()
        assert ae._set_hvac_mode.call_args.args[0] == "off"


# ---------------------------------------------------------------------------
# Fix 4: _start_grace_period sets _grace_end_time; expiry clears it
# ---------------------------------------------------------------------------


class TestGraceEndTime:
    """_start_grace_period() must populate _grace_end_time; grace expiry clears it."""

    def test_grace_end_time_set_on_start(self):
        """After _start_grace_period(), _grace_end_time is a parseable ISO string."""
        ae = _make_ae_stub()
        fixed_now = datetime(2026, 4, 5, 19, 0, 0)

        with (
            patch(
                "custom_components.climate_advisor.automation.async_call_later",
                return_value=MagicMock(),
            ),
            patch("custom_components.climate_advisor.automation.dt_util") as mock_dt,
        ):
            mock_dt.now.return_value = fixed_now
            ae._start_grace_period("manual")

        assert ae._grace_end_time is not None
        # Must be parseable as an ISO datetime (raises ValueError if not)
        parsed = datetime.fromisoformat(ae._grace_end_time)
        assert isinstance(parsed, datetime)

    def test_grace_end_time_cleared_on_expiry(self):
        """When the grace callback fires with no open sensors, _grace_end_time → None."""
        ae = _make_ae_stub()

        captured_callback = {}

        def _fake_async_call_later(_hass, _delay, cb):
            captured_callback["fn"] = cb
            return MagicMock()

        with (
            patch(
                "custom_components.climate_advisor.automation.async_call_later",
                side_effect=_fake_async_call_later,
            ),
            patch(
                "custom_components.climate_advisor.automation.callback",
                side_effect=lambda fn: fn,
            ),
        ):
            ae._start_grace_period("automation")

        assert ae._grace_end_time is not None
        assert ae._grace_active is True

        # Simulate grace expiry: no sensors open, no planned window period
        ae._sensor_check_callback = None  # no open sensors
        ae._is_within_planned_window_period = MagicMock(return_value=False)

        # Fire the captured _grace_expired callback
        captured_callback["fn"](None)

        assert ae._grace_end_time is None
        assert ae._grace_active is False


# ---------------------------------------------------------------------------
# Issue #817: _is_within_planned_window_period() prefers the ODE-adjusted
# nat-vent cutoff over the static classifier window_close_time
# ---------------------------------------------------------------------------


def _make_warm_classification(*, window_close_time) -> DayClassification:
    """Minimal warm-day, windows-recommended, HVAC-off DayClassification."""
    c = object.__new__(DayClassification)
    c.__dict__.update(
        {
            "day_type": "warm",
            "trend_direction": "stable",
            "trend_magnitude": 0,
            "today_high": 80,
            "today_low": 60,
            "tomorrow_high": 80,
            "tomorrow_low": 60,
            "hvac_mode": "off",
            "pre_condition": False,
            "pre_condition_target": 0.0,
            "windows_recommended": True,
            "window_open_time": time(6, 0),
            "window_close_time": window_close_time,
            "setback_modifier": 0.0,
            "window_opportunity_morning": False,
            "window_opportunity_evening": False,
        }
    )
    return c


class TestIsWithinPlannedWindowPeriodOdeCutoff:
    """This is a real control-boundary change (Issue #817), not display text: the
    door/window pause exemption must end at the ODE-adjusted cutoff — the exact
    number already shown on the briefing/status cards — not the static classifier
    hour, whenever the two disagree (the same #818 condition: ODE cutoff earlier
    than the raw classifier close time)."""

    def test_ode_cutoff_earlier_than_static_close_ends_exemption_early(self):
        """ODE cutoff (9:15 AM) < static window_close_time (10:00 AM). At 9:30 AM —
        after the ODE cutoff but still before the static close time — the exemption
        must already be OFF (door/window events should pause HVAC normally)."""
        ae = _make_ae_stub()
        ae._is_within_planned_window_period = types.MethodType(
            _ae_mod.AutomationEngine._is_within_planned_window_period, ae
        )
        ae._current_classification = _make_warm_classification(window_close_time=time(10, 0))
        ae._nat_vent_cutoff = datetime(2026, 7, 10, 9, 15, 0)

        with patch("custom_components.climate_advisor.automation.dt_util") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 7, 10, 9, 30, 0)
            assert ae._is_within_planned_window_period() is False

    def test_still_exempt_before_ode_cutoff(self):
        """Same setup, but evaluated at 9:00 AM — before the ODE cutoff — the
        exemption must still be active."""
        ae = _make_ae_stub()
        ae._is_within_planned_window_period = types.MethodType(
            _ae_mod.AutomationEngine._is_within_planned_window_period, ae
        )
        ae._current_classification = _make_warm_classification(window_close_time=time(10, 0))
        ae._nat_vent_cutoff = datetime(2026, 7, 10, 9, 15, 0)

        with patch("custom_components.climate_advisor.automation.dt_util") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 7, 10, 9, 0, 0)
            assert ae._is_within_planned_window_period() is True

    def test_falls_back_to_static_close_time_without_cutoff(self):
        """No ODE cutoff mirrored (e.g. before the first cycle, or a WARM day with no
        forecast curve) — falls back to the static window_close_time unchanged."""
        ae = _make_ae_stub()
        ae._is_within_planned_window_period = types.MethodType(
            _ae_mod.AutomationEngine._is_within_planned_window_period, ae
        )
        ae._current_classification = _make_warm_classification(window_close_time=time(10, 0))
        ae._nat_vent_cutoff = None

        with patch("custom_components.climate_advisor.automation.dt_util") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 7, 10, 9, 30, 0)
            assert ae._is_within_planned_window_period() is True

    def test_consumes_stabilized_cutoff_unchanged_by_reason_wording(self):
        """Issue #869: the coordinator's _stabilize_nat_vent_cutoff_reason() may hold
        back a reason-flip and propagate an older confirmed nat_vent_cutoff time
        instead of a raced/flapping one, but _is_within_planned_window_period() has
        no knowledge of *why* self._nat_vent_cutoff carries the value it does — it
        simply reads self._nat_vent_cutoff as-is. This test plugs in a specific
        stabilized (i.e. "held-back", would-be-earlier-if-unstabilized) cutoff
        value directly and confirms the boundary check still behaves exactly as
        documented: exempt strictly before the cutoff, not exempt at/after it.
        Doesn't duplicate the coordinator-level stabilization tests
        (tests/test_nat_vent_cutoff_stability.py) — only confirms this consumer
        side still works correctly with a stabilized value plugged in.
        """
        ae = _make_ae_stub()
        ae._is_within_planned_window_period = types.MethodType(
            _ae_mod.AutomationEngine._is_within_planned_window_period, ae
        )
        ae._current_classification = _make_warm_classification(window_close_time=time(11, 0))
        # Simulates a stabilized value: the coordinator's raw scan may have
        # already raced ahead to a later "outdoor_rise" cutoff, but the
        # sustain-confirm gate held the propagated value back at this earlier
        # "comfort_floor" cutoff since the flip hadn't yet sustained for 90s.
        stabilized_cutoff = datetime(2026, 7, 10, 9, 15, 0)
        ae._nat_vent_cutoff = stabilized_cutoff

        with patch("custom_components.climate_advisor.automation.dt_util") as mock_dt:
            # Just before the stabilized cutoff -> still exempt.
            mock_dt.now.return_value = datetime(2026, 7, 10, 9, 14, 59)
            assert ae._is_within_planned_window_period() is True

            # Exactly at the stabilized cutoff -> boundary is inclusive (`<=`).
            mock_dt.now.return_value = datetime(2026, 7, 10, 9, 15, 0)
            assert ae._is_within_planned_window_period() is True

            # Just after the stabilized cutoff -> no longer exempt, even though
            # the static classifier close time (11:00) hasn't arrived yet.
            mock_dt.now.return_value = datetime(2026, 7, 10, 9, 15, 1)
            assert ae._is_within_planned_window_period() is False


# ---------------------------------------------------------------------------
# Issue #1231 / #1218: the planned window period must END on time
# ---------------------------------------------------------------------------


class TestPlannedWindowPeriodEndsOnTime_1231:
    """Occupant: the briefing says "close windows by 17:00", but after 17:00 the Status card kept saying "windows
    open (as planned)" until 19:00-23:00 and the background open-window check was skipped for those hours. The
    forward-only nat-vent scan re-resolves the cutoff to "now + 1 h" on every recompute once the crossing is
    behind us (``nat_vent_cutoff_already_reached=True``), and the engine compared only the cutoff's time of day.
    """

    @staticmethod
    def _engine(*, static_close, cutoff, already_reached=False):
        ae = _make_ae_stub()
        ae._is_within_planned_window_period = types.MethodType(
            _ae_mod.AutomationEngine._is_within_planned_window_period, ae
        )
        ae._current_classification = _make_warm_classification(window_close_time=static_close)
        ae._nat_vent_cutoff = cutoff
        ae._nat_vent_cutoff_already_reached = already_reached
        return ae

    @staticmethod
    def _within(ae, now):
        with patch("custom_components.climate_advisor.automation.dt_util") as mock_dt:
            mock_dt.now.return_value = now
            return ae._is_within_planned_window_period()

    def test_already_reached_ends_the_period_even_though_cutoff_is_an_hour_ahead(self):
        now = datetime(2026, 7, 10, 18, 0, 0)
        ae = self._engine(static_close=time(17, 0), cutoff=datetime(2026, 7, 10, 19, 0, 0), already_reached=True)
        assert self._within(ae, now) is False

    def test_not_already_reached_cutoff_today_still_governs(self):
        ae = self._engine(static_close=time(17, 0), cutoff=datetime(2026, 7, 10, 19, 0, 0), already_reached=False)
        assert self._within(ae, datetime(2026, 7, 10, 18, 0, 0)) is True

    def test_cutoff_dated_tomorrow_early_falls_back_to_todays_static_close(self):
        ae = self._engine(static_close=time(17, 0), cutoff=datetime(2026, 7, 11, 11, 0, 0))
        assert self._within(ae, datetime(2026, 7, 10, 14, 0, 0)) is True

    def test_cutoff_dated_tomorrow_late_does_not_extend_todays_period(self):
        ae = self._engine(static_close=time(17, 0), cutoff=datetime(2026, 7, 11, 18, 0, 0))
        assert self._within(ae, datetime(2026, 7, 10, 17, 30, 0)) is False

    def test_cutoff_dated_yesterday_falls_back_to_todays_static_close(self):
        ae = self._engine(static_close=time(17, 0), cutoff=datetime(2026, 7, 9, 11, 0, 0))
        assert self._within(ae, datetime(2026, 7, 10, 14, 0, 0)) is True

    def test_before_open_time_is_not_within(self):
        ae = self._engine(static_close=time(17, 0), cutoff=None)
        assert self._within(ae, datetime(2026, 7, 10, 5, 59, 0)) is False

    def test_default_flag_value_on_a_fresh_engine_is_false(self):
        from tests.test_dry_run import _make_automation_engine

        assert _make_automation_engine()._nat_vent_cutoff_already_reached is False


def _reached_classification():
    return _make_warm_classification(window_close_time=time(17, 0))


class TestApplyClassificationStoresAlreadyReached_1231:
    def test_apply_classification_stores_the_flag(self):
        from tests.test_dry_run import _make_automation_engine

        engine = _make_automation_engine()
        cutoff = datetime(2026, 7, 10, 19, 0, 0)
        asyncio.run(
            engine.apply_classification(
                _reached_classification(), nat_vent_cutoff=cutoff, nat_vent_cutoff_already_reached=True
            )
        )
        assert engine._nat_vent_cutoff == cutoff
        assert engine._nat_vent_cutoff_already_reached is True

    def test_a_caller_passing_no_cutoff_resets_both(self):
        from tests.test_dry_run import _make_automation_engine

        engine = _make_automation_engine()
        engine._nat_vent_cutoff = datetime(2026, 7, 10, 19, 0, 0)
        engine._nat_vent_cutoff_already_reached = True
        asyncio.run(engine.apply_classification(_reached_classification()))
        assert engine._nat_vent_cutoff is None
        assert engine._nat_vent_cutoff_already_reached is False

    def test_the_engines_own_re_pass_keeps_the_flag(self):
        """Structural (AST): the reactive re-pass in automation.py re-applies the stored cutoff, so it must carry
        the stored flag too, else one reactive tick silently re-opens the period."""
        import ast
        from pathlib import Path

        src = Path(_ae_mod.__file__).read_text(encoding="utf-8")
        passes = []
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.Call) and getattr(node.func, "attr", None) == "apply_classification":
                kws = {k.arg: k.value for k in node.keywords}
                if "nat_vent_cutoff" in kws:
                    passes.append(
                        (node.lineno, ast.unparse(kws.get("nat_vent_cutoff_already_reached", ast.Constant(None))))
                    )
        assert passes, "expected the reactive re-pass call site"
        assert all(flag == "self._nat_vent_cutoff_already_reached" for _, flag in passes), passes


def _is_apply_classification_call(node) -> bool:  # noqa: ANN001
    """An apply_classification() call, direct or routed through decide(coordinator, engine.apply_classification, ...)
    (Issue #1443: the coordinator passes its facts in around the same call, with the same keywords)."""
    import ast  # noqa: PLC0415

    if not isinstance(node, ast.Call):
        return False
    if getattr(node.func, "attr", None) == "apply_classification":
        return True
    return (
        isinstance(node.func, ast.Name)
        and node.func.id in ("decide", "decide_sync")
        and len(node.args) > 1
        and getattr(node.args[1], "attr", None) == "apply_classification"
    )


class TestEveryProductionCallerKeepsThePlansClose_1231:
    def test_no_production_apply_classification_call_drops_the_cutoff_or_flag(self):
        """Structural (AST): a caller that passes neither value resets the engine to the static close, which
        re-opens a planned window period the plan already ended (grace expiry and the cancel-override follow-up
        did this until #1231). Every production call must carry both."""
        import ast
        from pathlib import Path

        pkg = Path(__file__).resolve().parents[1] / "custom_components" / "climate_advisor"
        missing, sites = [], 0
        for path in sorted(pkg.glob("*.py")):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if _is_apply_classification_call(node):
                    sites += 1
                    kws = {k.arg for k in node.keywords}
                    if not {"nat_vent_cutoff", "nat_vent_cutoff_already_reached"} <= kws:
                        missing.append(f"{path.name}:{node.lineno}")
        assert sites >= 7, sites
        assert not missing, missing


class TestCoordinatorPassesAlreadyReached_1231:
    def test_reassert_setpoint_now_passes_the_flag(self):
        import importlib

        mod = importlib.import_module("custom_components.climate_advisor.coordinator")
        coord = MagicMock()
        coord.automation_engine.apply_classification = AsyncMock()
        coord._current_classification = _reached_classification()
        coord._last_predicted_indoor = []
        coord._get_indoor_temp = MagicMock(return_value=72.0)
        cutoff = datetime(2026, 7, 10, 19, 0, 0)
        coord._nat_vent_plan = {"nat_vent_cutoff": cutoff, "nat_vent_cutoff_already_reached": True}
        method = types.MethodType(mod.ClimateAdvisorCoordinator._async_reassert_setpoint_now, coord)
        asyncio.run(method("test"))
        kw = coord.automation_engine.apply_classification.await_args.kwargs
        assert kw["nat_vent_cutoff"] == cutoff
        assert kw["nat_vent_cutoff_already_reached"] is True

    def test_every_coordinator_site_that_passes_a_cutoff_also_passes_the_flag(self):
        """Structural: a new/edited call site cannot pass the cutoff without the flag (AST, not a re-implementation)."""
        import ast
        from pathlib import Path

        src = Path(__file__).resolve().parents[1] / "custom_components" / "climate_advisor" / "coordinator.py"
        sites = 0
        for node in ast.walk(ast.parse(src.read_text(encoding="utf-8"))):
            if _is_apply_classification_call(node):
                kws = {k.arg for k in node.keywords}
                if "nat_vent_cutoff" in kws:
                    sites += 1
                    assert "nat_vent_cutoff_already_reached" in kws, f"line {node.lineno}: cutoff without the flag"
        # 5 since Issue #1405: the Home/Guest bank-end timer (_fire_tou_home_bank_end) runs the regular cycle's own
        # pass at the cost window's start, so it passes the same cutoff AND flag (checked above).
        assert sites == 5, f"expected 5 coordinator sites, found {sites}"


class TestExitEdgeFiresOnceWhenPeriodEndsViaAlreadyReached_1231:
    """The exit-edge reconcile (pauses HVAC for a window left open) must still fire exactly once when the period
    ends because the plan reports the crossing already behind us."""

    def test_edge_fires_once_then_not_again(self):
        ae = _make_ae_stub()
        for name in ("_is_within_planned_window_period", "consume_planned_window_exit_edge"):
            setattr(ae, name, types.MethodType(getattr(_ae_mod.AutomationEngine, name), ae))
        ae._planned_window_was_active = False
        ae._current_classification = _make_warm_classification(window_close_time=time(17, 0))
        ae._nat_vent_cutoff = datetime(2026, 7, 10, 16, 30, 0)
        ae._nat_vent_cutoff_already_reached = False
        with patch("custom_components.climate_advisor.automation.dt_util") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 7, 10, 16, 0, 0)
            assert ae.consume_planned_window_exit_edge() is False  # period starts active: no edge
            assert ae._planned_window_was_active is True
            mock_dt.now.return_value = datetime(2026, 7, 10, 17, 5, 0)
            ae._nat_vent_cutoff = datetime(2026, 7, 10, 18, 0, 0)  # next grid point after "now"
            ae._nat_vent_cutoff_already_reached = True
            assert ae.consume_planned_window_exit_edge() is True
            assert ae.consume_planned_window_exit_edge() is False
