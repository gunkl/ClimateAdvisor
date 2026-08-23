"""Tests for grace stuck-at-0 self-healing (Bug 2, Issue #321).

Two aspects tested:
  1. AutomationEngine._cancel_grace_timers() now clears _grace_end_time.
  2. Coordinator _async_update_data stuck-grace guard detects stale _grace_end_time
     in the past when _grace_active=False and force-clears the override.

Occupant framing: if the grace expiry callback was ever lost (HA restart, exception),
the dashboard showed "0 min remaining" forever and automation never resumed. The user
had to click Resume manually to get CA back in control.
"""

from __future__ import annotations

import importlib
import sys
import types
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

# ── HA module stubs (must happen before importing climate_advisor) ──
if "homeassistant" not in sys.modules:
    from conftest import install_ha_stubs

    install_ha_stubs()

_STABLE_NOW = datetime(2026, 6, 12, 14, 0, 0)
sys.modules["homeassistant.util.dt"].now = lambda: _STABLE_NOW

from custom_components.climate_advisor.automation import AutomationEngine  # noqa: E402
from custom_components.climate_advisor.classifier import DayClassification  # noqa: E402
from custom_components.climate_advisor.learning import DailyRecord  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_THERMOSTAT_ID = "climate.thermostat"
_PATCH_DT_NOW = "custom_components.climate_advisor.coordinator.dt_util.now"


def _get_coordinator_class():
    """Return the current ClimateAdvisorCoordinator class — avoids stale __globals__."""
    mod = importlib.import_module("custom_components.climate_advisor.coordinator")
    return mod.ClimateAdvisorCoordinator


def _consume_coroutine(coro):
    """Close coroutine to prevent 'never awaited' RuntimeWarning."""
    coro.close()


def _make_automation_engine_stub() -> AutomationEngine:
    """Create a bare AutomationEngine stub via object.__new__ (no __init__)."""
    hass = MagicMock()
    hass.services = MagicMock()
    hass.services.async_call = AsyncMock()
    hass.async_create_task = MagicMock(side_effect=_consume_coroutine)

    ae = object.__new__(AutomationEngine)
    ae.hass = hass
    ae.climate_entity = _THERMOSTAT_ID
    ae.config = {
        "comfort_heat": 70,
        "comfort_cool": 75,
    }
    ae._grace_active = False
    ae._grace_end_time = None
    ae._grace_duration_seconds = 0
    ae._last_resume_source = None
    ae._last_grace_trigger = None
    ae._manual_grace_cancel = None
    ae._automation_grace_cancel = None
    ae._manual_override_active = False
    ae._manual_override_mode = None
    ae._manual_override_source = None
    ae._manual_override_time = None
    ae._natural_vent_active = False
    ae._fan_active = False
    ae._fan_override_active = False
    ae.clear_manual_override = MagicMock()
    # Issue #757 Phase 6 Step 3: _resolve_override_grace_fsm_state() is now
    # unconditionally FSM-authoritative, so it always reads these 3 fields via
    # override_grace_lifecycle_state plus the extra fields _build_override_grace_fsm_
    # inputs() reads — previously only exercised when a test explicitly set
    # _override_grace_fsm_authoritative=True, now exercised unconditionally.
    ae._override_confirm_pending = False
    ae._override_confirm_source = None
    ae._override_confirm_mode = None
    ae._grace_protects_override = False
    ae._current_classification = None
    ae._sensor_check_callback = None
    ae._emit_event_callback = None
    return ae


def _make_classification(**overrides):
    c = object.__new__(DayClassification)
    defaults = {
        "day_type": "warm",
        "trend_direction": "stable",
        "trend_magnitude": 0,
        "today_high": 78,
        "today_low": 58,
        "tomorrow_high": 79,
        "tomorrow_low": 59,
        "hvac_mode": "cool",
        "pre_condition": False,
        "pre_condition_target": None,
        "windows_recommended": False,
        "window_open_time": None,
        "window_close_time": None,
        "setback_modifier": 0.0,
    }
    defaults.update(overrides)
    c.__dict__.update(defaults)
    return c


def _make_today_record(**overrides) -> DailyRecord:
    kwargs = dict(date="2026-06-12", day_type="warm", trend_direction="stable")
    kwargs.update(overrides)
    return DailyRecord(**kwargs)


def _make_stuck_grace_coord_stub(
    *,
    manual_override_active: bool = True,
    grace_active: bool = False,
    grace_end_time: str | None = None,
) -> object:
    """Build a minimal coordinator stub for stuck-grace detection tests."""
    hass = MagicMock()
    hass.services = MagicMock()
    hass.services.async_call = AsyncMock()
    hass.async_create_task = MagicMock(side_effect=_consume_coroutine)

    ClimateAdvisorCoordinator = _get_coordinator_class()
    coord = object.__new__(ClimateAdvisorCoordinator)
    coord.hass = hass
    coord.config = {
        "climate_entity": _THERMOSTAT_ID,
        "comfort_heat": 70,
        "comfort_cool": 75,
    }

    ae = MagicMock()
    ae._natural_vent_active = False
    ae._fan_active = False
    ae._fan_override_active = False
    ae._manual_override_active = manual_override_active
    ae._grace_active = grace_active
    ae._grace_end_time = grace_end_time
    # Issue #530: MagicMock attributes are truthy by default (CLAUDE.md testing doctrine) —
    # must be set explicitly or _check_orphaned_grace()'s new _grace_protects_override gate
    # would pass for the wrong reason. Default True here matches every pre-#530 test's intent
    # in this file: they all model a grace that protects a real override.
    ae._grace_protects_override = True
    ae.clear_manual_override = MagicMock()
    coord.automation_engine = ae

    coord._current_classification = _make_classification()
    coord._today_record = _make_today_record()
    coord._async_save_state = AsyncMock()
    coord._emit_event = MagicMock()

    return coord


# ---------------------------------------------------------------------------
# TestCancelGraceTimersClearsEndTime: Bug 2 fix in AutomationEngine
# ---------------------------------------------------------------------------


class TestCancelGraceTimersClearsEndTime:
    """_cancel_grace_timers clears _grace_end_time (Bug 2 fix)."""

    def test_cancel_grace_timers_clears_grace_end_time(self):
        """_cancel_grace_timers must set _grace_end_time to None.

        Before the fix: _grace_end_time was left set; the dashboard showed
        '0 min remaining' permanently, blocking automation from showing its
        next action.
        """
        ae = _make_automation_engine_stub()
        ae._grace_end_time = "2026-06-12T14:30:00"
        ae._grace_active = True
        ae._manual_grace_cancel = None
        ae._automation_grace_cancel = None

        ae._cancel_grace_timers = types.MethodType(AutomationEngine._cancel_grace_timers, ae)
        ae._cancel_grace_timers()

        assert ae._grace_end_time is None

    def test_cancel_grace_timers_sets_grace_active_false(self):
        """_cancel_grace_timers clears the grace-active flag."""
        ae = _make_automation_engine_stub()
        ae._grace_end_time = "2026-06-12T14:30:00"
        ae._grace_active = True

        ae._cancel_grace_timers = types.MethodType(AutomationEngine._cancel_grace_timers, ae)
        ae._cancel_grace_timers()

        assert ae._grace_active is False

    def test_cancel_grace_timers_calls_cancel_callbacks(self):
        """_cancel_grace_timers invokes both cancel callbacks when present."""
        ae = _make_automation_engine_stub()
        mock_manual = MagicMock()
        mock_auto = MagicMock()
        ae._manual_grace_cancel = mock_manual
        ae._automation_grace_cancel = mock_auto
        ae._grace_active = True
        ae._grace_end_time = "2026-06-12T14:30:00"

        ae._cancel_grace_timers = types.MethodType(AutomationEngine._cancel_grace_timers, ae)
        ae._cancel_grace_timers()

        mock_manual.assert_called_once()
        mock_auto.assert_called_once()
        assert ae._manual_grace_cancel is None
        assert ae._automation_grace_cancel is None

    def test_cancel_grace_timers_clears_last_resume_source(self):
        """_cancel_grace_timers resets _last_resume_source."""
        ae = _make_automation_engine_stub()
        ae._last_resume_source = "manual"
        ae._grace_active = True
        ae._grace_end_time = "2026-06-12T14:30:00"

        ae._cancel_grace_timers = types.MethodType(AutomationEngine._cancel_grace_timers, ae)
        ae._cancel_grace_timers()

        assert ae._last_resume_source is None

    def test_cancel_grace_timers_noop_when_no_timers(self):
        """_cancel_grace_timers is safe to call with no active timers."""
        ae = _make_automation_engine_stub()
        # All None and False — should not raise
        ae._cancel_grace_timers = types.MethodType(AutomationEngine._cancel_grace_timers, ae)
        ae._cancel_grace_timers()
        assert ae._grace_active is False
        assert ae._grace_end_time is None


# ---------------------------------------------------------------------------
# TestStuckGraceDetection: coordinator _async_update_data stuck-grace guard
# ---------------------------------------------------------------------------


class TestStuckGraceDetection:
    """Coordinator detects and clears stuck grace in _async_update_data."""

    def _simulate_stuck_grace_check(self, coord, now: datetime) -> None:
        """Replicate the stuck-grace guard logic from _async_update_data.

        This method mirrors the if-block added in coordinator.py under Bug 2 (Issue #321):

            ae = self.automation_engine
            if ae._manual_override_active and not ae._grace_active:
                end_time_str = getattr(ae, "_grace_end_time", None)
                if end_time_str is not None:
                    try:
                        end_dt = datetime.fromisoformat(end_time_str)
                        if end_dt.tzinfo is None:
                            end_dt = end_dt.replace(tzinfo=UTC)
                        now_utc = now.replace(tzinfo=UTC) if now.tzinfo is None else now
                        if end_dt < now_utc:
                            _LOGGER.error("Stuck grace: ...")
                            ae.clear_manual_override(reason="stuck_grace_recovery")
                            self._emit_event("stuck_grace_recovered", {...})
                    except (ValueError, TypeError):
                        pass
        """
        import logging

        _LOGGER = logging.getLogger("custom_components.climate_advisor.coordinator")
        ae = coord.automation_engine
        if ae._manual_override_active and not ae._grace_active:
            end_time_str = getattr(ae, "_grace_end_time", None)
            if end_time_str is not None:
                try:
                    end_dt = datetime.fromisoformat(end_time_str)
                    if end_dt.tzinfo is None:
                        end_dt = end_dt.replace(tzinfo=UTC)
                    now_utc = now.replace(tzinfo=UTC) if now.tzinfo is None else now
                    if end_dt < now_utc:
                        _LOGGER.error(
                            "Stuck grace detected: grace_end_time=%s is in the past but"
                            " grace_active=False and override still set — force-clearing",
                            end_time_str,
                        )
                        ae.clear_manual_override(reason="stuck_grace_recovery")
                        coord._emit_event(
                            "stuck_grace_recovered",
                            {"grace_end_time": end_time_str},
                        )
                except (ValueError, TypeError):
                    pass

    def test_stuck_grace_clears_override(self):
        """When grace_end_time is past and grace_active=False, clear_manual_override is called.

        Occupant impact: automation had been blocked indefinitely because the grace
        expiry callback was lost. The self-healing guard restores normal automation.
        """
        past_ts = (datetime(2026, 6, 12, 13, 0, 0, tzinfo=UTC)).isoformat()
        coord = _make_stuck_grace_coord_stub(
            manual_override_active=True,
            grace_active=False,
            grace_end_time=past_ts,
        )

        self._simulate_stuck_grace_check(coord, datetime(2026, 6, 12, 14, 0, 0))

        coord.automation_engine.clear_manual_override.assert_called_once_with(reason="stuck_grace_recovery")

    def test_stuck_grace_event_emitted(self):
        """stuck_grace_recovered event is emitted when the guard fires."""
        past_ts = (datetime(2026, 6, 12, 13, 0, 0, tzinfo=UTC)).isoformat()
        coord = _make_stuck_grace_coord_stub(
            manual_override_active=True,
            grace_active=False,
            grace_end_time=past_ts,
        )

        self._simulate_stuck_grace_check(coord, datetime(2026, 6, 12, 14, 0, 0))

        coord._emit_event.assert_called_once()
        event_name, event_data = coord._emit_event.call_args[0]
        assert event_name == "stuck_grace_recovered"
        assert event_data["grace_end_time"] == past_ts

    def test_no_stuck_grace_when_grace_active(self):
        """When _grace_active=True, timer is still running — no self-heal needed."""
        past_ts = (datetime(2026, 6, 12, 13, 0, 0, tzinfo=UTC)).isoformat()
        coord = _make_stuck_grace_coord_stub(
            manual_override_active=True,
            grace_active=True,  # timer still running — not stuck
            grace_end_time=past_ts,
        )

        self._simulate_stuck_grace_check(coord, datetime(2026, 6, 12, 14, 0, 0))

        coord.automation_engine.clear_manual_override.assert_not_called()
        coord._emit_event.assert_not_called()

    def test_no_stuck_grace_when_grace_end_time_none(self):
        """When _grace_end_time is None, there is no stuck grace to detect."""
        coord = _make_stuck_grace_coord_stub(
            manual_override_active=True,
            grace_active=False,
            grace_end_time=None,
        )

        self._simulate_stuck_grace_check(coord, datetime(2026, 6, 12, 14, 0, 0))

        coord.automation_engine.clear_manual_override.assert_not_called()
        coord._emit_event.assert_not_called()

    def test_no_stuck_grace_when_end_time_in_future(self):
        """When _grace_end_time is in the future, grace is not stuck."""
        future_ts = (datetime(2026, 6, 12, 15, 0, 0, tzinfo=UTC)).isoformat()
        coord = _make_stuck_grace_coord_stub(
            manual_override_active=True,
            grace_active=False,
            grace_end_time=future_ts,
        )

        self._simulate_stuck_grace_check(coord, datetime(2026, 6, 12, 14, 0, 0))

        coord.automation_engine.clear_manual_override.assert_not_called()
        coord._emit_event.assert_not_called()

    def test_no_stuck_grace_when_no_override(self):
        """When _manual_override_active=False, stuck-grace guard does not fire."""
        past_ts = (datetime(2026, 6, 12, 13, 0, 0, tzinfo=UTC)).isoformat()
        coord = _make_stuck_grace_coord_stub(
            manual_override_active=False,
            grace_active=False,
            grace_end_time=past_ts,
        )

        self._simulate_stuck_grace_check(coord, datetime(2026, 6, 12, 14, 0, 0))

        coord.automation_engine.clear_manual_override.assert_not_called()
        coord._emit_event.assert_not_called()


# ---------------------------------------------------------------------------
# TestOrphanedGraceDetection: coordinator._check_orphaned_grace() (Issue #508)
#
# Mirror shape of Issue #321's stuck-grace check, but the opposite condition:
# grace_active=True with no override active (rather than grace_active=False with
# an override stuck true past its own due grace_end_time). Unlike the tests above,
# these invoke the REAL coordinator._check_orphaned_grace() method directly instead
# of re-implementing its logic in the test — see CLAUDE.md's "never mirror the logic
# under test" doctrine (Issue #434).
# ---------------------------------------------------------------------------


class TestOrphanedGraceDetection:
    """coordinator._check_orphaned_grace() self-heals a grace period protecting nothing."""

    def test_orphaned_grace_is_cancelled(self):
        """grace_active=True with no override active — grace is force-cancelled.

        Occupant impact: this is Issue #508's actual bug shape — a user cancelled a fan
        override via the dashboard but the endpoint (before the fix) never cancelled grace.
        This watchdog is the defense-in-depth backstop for any future path that reproduces
        that gap.
        """
        coord = _make_stuck_grace_coord_stub(
            manual_override_active=False,
            grace_active=True,
            grace_end_time="2026-06-12T21:53:00+00:00",
        )
        coord.automation_engine._fan_override_active = False
        coord.automation_engine._cancel_grace_timers_action = MagicMock()

        coord._check_orphaned_grace()

        coord.automation_engine._cancel_grace_timers_action.assert_called_once()
        coord._emit_event.assert_called_once()
        event_name, event_data = coord._emit_event.call_args[0]
        assert event_name == "stuck_grace_recovered"
        assert event_data["reason"] == "grace_without_override"
        assert event_data["grace_end_time"] == "2026-06-12T21:53:00+00:00"

    def test_orphaned_grace_not_cancelled_when_manual_override_active(self):
        """grace_active=True WITH a manual override active — must NOT fire.

        A false-positive here would kill a legitimate in-progress override's grace period.
        """
        coord = _make_stuck_grace_coord_stub(
            manual_override_active=True,
            grace_active=True,
            grace_end_time="2026-06-12T21:53:00+00:00",
        )
        coord.automation_engine._fan_override_active = False
        coord.automation_engine._cancel_grace_timers_action = MagicMock()

        coord._check_orphaned_grace()

        coord.automation_engine._cancel_grace_timers_action.assert_not_called()
        coord._emit_event.assert_not_called()

    def test_orphaned_grace_not_cancelled_when_fan_override_active(self):
        """grace_active=True WITH a fan override active — must NOT fire.

        This is the fan-only-override shape (Root cause #1 of Issue #508): _manual_override_
        active is False even though a real, legitimate override is in progress.
        """
        coord = _make_stuck_grace_coord_stub(
            manual_override_active=False,
            grace_active=True,
            grace_end_time="2026-06-12T21:53:00+00:00",
        )
        coord.automation_engine._fan_override_active = True
        coord.automation_engine._cancel_grace_timers_action = MagicMock()

        coord._check_orphaned_grace()

        coord.automation_engine._cancel_grace_timers_action.assert_not_called()
        coord._emit_event.assert_not_called()

    def test_no_orphaned_grace_when_grace_inactive(self):
        """grace_active=False — nothing to do regardless of override state."""
        coord = _make_stuck_grace_coord_stub(
            manual_override_active=False,
            grace_active=False,
            grace_end_time=None,
        )
        coord.automation_engine._fan_override_active = False
        coord.automation_engine._cancel_grace_timers_action = MagicMock()

        coord._check_orphaned_grace()

        coord.automation_engine._cancel_grace_timers_action.assert_not_called()
        coord._emit_event.assert_not_called()

    def test_orphaned_grace_not_cancelled_when_grace_does_not_protect_override(self):
        """Issue #530: grace_active=True, no override flags, BUT this grace was never
        started to protect an override (e.g. fan-off cooldown, window-close resume) —
        must NOT fire. Pre-#530, this exact shape (no override flag set, by design) was
        indistinguishable from a genuinely orphaned override-grace, so a fan-off grace
        was killed within ~1 event-loop tick of starting, defeating Issue #359's
        fan-off protection almost universally, not just in the RF-timer scenario #530
        was originally reported against.
        """
        coord = _make_stuck_grace_coord_stub(
            manual_override_active=False,
            grace_active=True,
            grace_end_time="2026-06-12T21:53:00+00:00",
        )
        coord.automation_engine._fan_override_active = False
        coord.automation_engine._grace_protects_override = False
        coord.automation_engine._cancel_grace_timers_action = MagicMock()

        coord._check_orphaned_grace()

        coord.automation_engine._cancel_grace_timers_action.assert_not_called()
        coord._emit_event.assert_not_called()

    def test_orphaned_grace_still_cancelled_when_protects_override_and_flags_gone(self):
        """Issue #530 regression guard: the new gate must not accidentally weaken the
        original Issue #508 protection. A grace that WAS started to protect a real
        override, whose override flags are now gone (the actual bug #508 exists to
        catch), must still be force-cancelled."""
        coord = _make_stuck_grace_coord_stub(
            manual_override_active=False,
            grace_active=True,
            grace_end_time="2026-06-12T21:53:00+00:00",
        )
        coord.automation_engine._fan_override_active = False
        coord.automation_engine._grace_protects_override = True
        coord.automation_engine._cancel_grace_timers_action = MagicMock()

        coord._check_orphaned_grace()

        coord.automation_engine._cancel_grace_timers_action.assert_called_once()
        coord._emit_event.assert_called_once()


class TestOrphanedGraceRefreshesDoorWindowFsm:
    """Issue #679: ``_check_orphaned_grace()`` force-cancels a stuck grace and notifies
    the override/grace diagnostic FSM (Issue #639), but ``derive_door_window_lifecycle_state()``
    also takes ``grace_active`` as an input — this method previously never told the
    door/window diagnostic FSM (Issue #637) that ``grace_active`` had just flipped to
    False, leaving ``coordinator._door_window_fsm_state`` stuck at whatever stale value
    it held (e.g. ``GRACE``/``PAUSED_DURING_GRACE``) for up to 10 minutes, until an
    unrelated later event happened to resync it.

    Occupant impact: purely diagnostic (this FSM never writes back to production HVAC/
    fan/grace/override decisions) — but a stale door/window FSM state means the Debug
    tab's shadow-engine comparison misreports the door/window pause lifecycle as still
    mid-grace right after the real grace was force-cancelled, which would misdirect any
    developer using that diagnostic to debug a real grace/pause issue.

    Uses the real headless coordinator (production ``AutomationEngine`` + the real
    ``transition()`` function), not a mocked engine — a ``MagicMock`` automation_engine
    can't produce real ``DoorWindowFsmInputs``, so asserting the FSM's *computed* output
    state requires the real production engine, per CLAUDE.md's no-mirror-tests doctrine.
    """

    def test_door_window_fsm_state_resyncs_immediately_after_orphaned_grace_cancel(self) -> None:
        from custom_components.climate_advisor.door_window_lifecycle import DoorWindowLifecycleState
        from tools.sim_harness.build_coordinator import build_headless_coordinator

        coordinator, _fake_hass, _scheduler, _event_log = build_headless_coordinator()
        ae = coordinator.automation_engine

        # Build the exact stuck-grace shape _check_orphaned_grace() self-heals: grace
        # active, protecting an override, but no override actually active anymore.
        ae._grace_active = True
        ae._grace_protects_override = True
        ae._manual_override_active = False
        ae._fan_override_active = False
        ae._grace_end_time = "2024-01-15T09:00:00+00:00"

        # Simulate the door/window FSM having been left at a stale mid-grace value by
        # an earlier event, before this method's fix would have refreshed it. GRACE (not
        # PAUSED_DURING_GRACE) matches the realistic orphaned-grace shape: no door/window
        # is paused (ae._paused_by_door is False, matching production's real state here),
        # just a grace timer left dangling with nothing left to protect.
        coordinator._door_window_fsm_state = DoorWindowLifecycleState.GRACE

        coordinator._check_orphaned_grace()

        # Without the fix, this stays PAUSED_DURING_GRACE — derive_door_window_lifecycle_state()
        # is never re-run, so the diagnostic FSM keeps reporting stale grace state even
        # though ae._grace_active is now False.
        assert coordinator._door_window_fsm_state == DoorWindowLifecycleState.NORMAL


class TestGraceProtectsOverrideClassification:
    """automation._start_grace_period() sets _grace_protects_override from `trigger`
    membership in _GRACE_TRIGGERS_PROTECTING_OVERRIDE (Issue #530) — centralized
    classification instead of a boolean threaded through every callsite. Exercises the
    REAL AutomationEngine._start_grace_period(), not a re-implementation.
    """

    def _make_real_engine_for_grace_start(self):
        ae = _make_automation_engine_stub()
        ae._start_grace_period = types.MethodType(AutomationEngine._start_grace_period, ae)
        ae._cancel_grace_timers = types.MethodType(AutomationEngine._cancel_grace_timers, ae)
        ae._resolve_override_grace_fsm_state = types.MethodType(AutomationEngine._resolve_override_grace_fsm_state, ae)
        # Issue #672: _start_grace_period() now routes through the dispatcher, which
        # reads this flag directly (not via a bound-method default) even in legacy mode.
        ae._override_grace_fsm_authoritative = False
        ae._emit_event_callback = None
        ae.config = {}
        return ae

    def test_fan_manual_override_trigger_protects_override(self):
        """trigger='fan_manual_override' (a real fan-on override) -> protects_override=True.

        Issue #757 Phase 6 Step 3 correction: real production code never calls the
        generic ``_start_grace_period()`` wrapper with a protecting trigger — the 3
        protecting triggers (fan_manual_override, override_confirmed, dashboard_resume)
        call ``_start_grace_period_action()`` directly and dispatch their own specific
        FSM kind (see ``_start_grace_period()``'s own docstring). The wrapper always
        dispatches ``UNPROTECTED_GRACE_STARTED``, which the FSM unconditionally lands
        on ACTIVE_UNPROTECTED regardless of ``trigger`` — so routing a protecting
        trigger through the wrapper no longer reflects any real call shape (it only
        worked before because the legacy flag-write was trigger-string-keyed,
        independent of which FSM kind — if any — was notionally dispatched). This test
        now exercises the real, still-live classification function directly —
        ``_legacy_set_grace_flags()``, used by every non-FSM-modeled ``_start_grace_period()``
        caller and by ``_confirm_override()`` — instead of the wrapper."""
        ae = self._make_real_engine_for_grace_start()
        ae._legacy_set_grace_flags("fan_manual_override")
        assert ae._grace_protects_override is True

    def test_override_confirmed_trigger_protects_override(self):
        """trigger='override_confirmed' (a real thermostat override) -> protects_override=True.

        See test_fan_manual_override_trigger_protects_override's docstring for why this
        exercises _legacy_set_grace_flags() directly rather than the wrapper."""
        ae = self._make_real_engine_for_grace_start()
        ae._legacy_set_grace_flags("override_confirmed")
        assert ae._grace_protects_override is True

    def test_fan_off_trigger_does_not_protect_override(self):
        """trigger='fan_off' (Issue #359 cooldown, no override involved) -> protects_override=False."""
        ae = self._make_real_engine_for_grace_start()
        ae._start_grace_period("manual", trigger="fan_off")
        assert ae._grace_protects_override is False

    def test_dashboard_resume_trigger_does_not_protect_override(self):
        """trigger='dashboard_resume' -> protects_override=False."""
        ae = self._make_real_engine_for_grace_start()
        ae._start_grace_period("manual", trigger="dashboard_resume")
        assert ae._grace_protects_override is False

    def test_sensor_closed_resume_trigger_does_not_protect_override(self):
        """trigger='sensor_closed_resume' (automation-initiated) -> protects_override=False."""
        ae = self._make_real_engine_for_grace_start()
        ae._start_grace_period("automation", trigger="sensor_closed_resume")
        assert ae._grace_protects_override is False

    def test_cancel_grace_timers_resets_protects_override(self):
        """_cancel_grace_timers() must reset _grace_protects_override alongside _grace_active,
        so a stale True doesn't leak into whatever starts next."""
        ae = self._make_real_engine_for_grace_start()
        ae._legacy_set_grace_flags("fan_manual_override")
        assert ae._grace_protects_override is True

        ae._cancel_grace_timers()

        assert ae._grace_protects_override is False
